"""Bounded, failure-directed course alternatives; no provider calls here."""

from collections import OrderedDict

from spontaneous.course import (
    ROLE_MAX_REQUIRED_STOPS,
    build_course_stop,
    calculate_distance_meters,
    can_cover_required_themes_for_role,
    get_place_identity,
    get_place_themes,
    get_required_themes_by_role,
    normalize_course_orders,
    place_ranking_key,
    select_best_covering_place,
)


MAX_CANDIDATES_PER_ROLE = 5
MAX_COURSE_ATTEMPTS = 20


def rank_course_candidates(
    grouped_places: dict[str, list[dict]],
    desired_themes: set[str],
    start_location,
) -> dict[str, list[dict]]:
    """Keep the existing coverage/score/distance ranking and unique identities."""
    required = get_required_themes_by_role(desired_themes)
    ranked = {}
    for role, places in grouped_places.items():
        seen = set()
        candidates = []
        for place in sorted(places, key=lambda item: place_ranking_key(
            item, desired_themes, role, start_location,
            remaining_themes=required.get(role),
        )):
            identity = get_place_identity(place)
            if identity not in seen:
                seen.add(identity)
                candidates.append(place)
        limited = candidates[:MAX_CANDIDATES_PER_ROLE]
        themes = required.get(role, set())
        if not can_cover_required_themes_for_role(limited, role, themes):
            # Do not let five similar high scores crowd out the only candidate
            # for another required ACTIVITY theme. Reserve a covering subset,
            # fill the remaining slots by rank, and keep the final list ranked.
            reserved = set()
            remaining = set(themes)
            while remaining and len(reserved) < MAX_CANDIDATES_PER_ROLE:
                selected = select_best_covering_place(
                    candidates, desired_themes, role, start_location, remaining, reserved,
                )
                if selected is None:
                    break
                reserved.add(get_place_identity(selected))
                remaining -= get_place_themes(selected)
            for place in candidates:
                if len(reserved) >= MAX_CANDIDATES_PER_ROLE:
                    break
                reserved.add(get_place_identity(place))
            limited = [place for place in candidates if get_place_identity(place) in reserved]
        ranked[role] = limited
    return ranked


def course_identity(course: list[dict]) -> tuple:
    # Order matters: it changes the origin and departure time of each leg.
    return tuple((stop["role"], get_place_identity(stop)) for stop in course)


class CourseCandidateSearch:
    def __init__(self, initial_course, ranked_candidates, desired_themes,
                 start_location, role_plan):
        self.candidates = ranked_candidates
        self.desired_themes = desired_themes
        self.start_location = start_location
        self.role_plan = role_plan
        self.required = get_required_themes_by_role(desired_themes)
        self.pending = OrderedDict([(course_identity(initial_course), initial_course)])
        self.visited = set()
        self.attempts = 0

    def next_course(self) -> list[dict] | None:
        if not self.pending or self.attempts >= MAX_COURSE_ATTEMPTS:
            return None
        identity, course = self.pending.popitem(last=False)
        self.visited.add(identity)
        self.attempts += 1
        return course

    def _role_options(self, role, stay_minutes, cursor):
        candidates = self.candidates.get(role, [])
        required_themes = self.required.get(role, set())
        if not required_themes:
            options = [[build_course_stop(
                place, 1, role, stay_minutes, self.desired_themes, cursor, False,
            )] for place in sorted(candidates, key=lambda item: place_ranking_key(
                item, self.desired_themes, role, cursor,
            ))]
            # Exhaust same-role replacements before dropping an optional role.
            return options + [[]]

        options = []
        max_stops = ROLE_MAX_REQUIRED_STOPS.get(role) or len(candidates)

        def cover(selected, remaining, location, used):
            if not remaining:
                options.append(selected)
                return
            if len(selected) >= max_stops:
                return
            for place in sorted(candidates, key=lambda item: place_ranking_key(
                item, self.desired_themes, role, location, remaining_themes=remaining,
            )):
                identity = get_place_identity(place)
                coverage = remaining & get_place_themes(place)
                if identity in used or not coverage:
                    continue
                stop = build_course_stop(
                    place, len(selected) + 1, role, stay_minutes,
                    self.desired_themes, location, True, coverage,
                )
                cover(selected + [stop], remaining - coverage, stop, used | {identity})

        # At most five candidates: <= 325 ordered subsets, without network I/O.
        # Each additional ACTIVITY must cover a previously uncovered theme.
        cover([], required_themes, cursor, set())
        return options

    def _replace_role(self, course, role, stops):
        blocks = {planned_role: [] for planned_role, _ in self.role_plan}
        for stop in course:
            blocks[stop["role"]].append(stop)
        blocks[role] = stops
        return normalize_course_orders([
            stop for planned_role, _ in self.role_plan for stop in blocks[planned_role]
        ])

    def _distance(self, course):
        locations = [self.start_location, *course, self.start_location]
        return sum(calculate_distance_meters(origin, destination)
                   for origin, destination in zip(locations, locations[1:]))

    def retry(self, course: list[dict], reason: str, failed_index: int | None = None) -> None:
        roles = [role for role, _ in self.role_plan]
        role_order = {role: index for index, role in enumerate(roles)}
        present_roles = {stop["role"] for stop in course}
        if failed_index is not None:
            preferred = [course[failed_index]["role"]]
            # An unroutable edge can also be fixed by replacing its origin.
            if reason == "NO_ROUTE" and failed_index > 0:
                origin_role = course[failed_index - 1]["role"]
                roles = [origin_role] + [role for role in roles if role != origin_role]
        elif reason == "RETURN_TIME_EXCEEDED":
            preferred = [role for role in reversed(roles)
                         if role not in self.required and role in present_roles]
            if not preferred:
                preferred = [role for role in roles if role in self.required]
        else:
            preferred = [role for role, themes in self.required.items()
                         if not themes.issubset(set().union(*(
                             get_place_themes(stop) for stop in course
                             if stop["role"] == role
                         )))]

        urgent = OrderedDict()
        for role in dict.fromkeys([*preferred, *roles]):
            if course and role not in self.required and role not in present_roles:
                # Once omitted, keep this branch lean while repairing required
                # stops. Earlier branches still retain optional alternatives.
                continue
            cursor = self.start_location
            for stop in course:
                if stop["role"] == role:
                    break
                if role_order[stop["role"]] < role_order[role]:
                    cursor = stop
            stay_minutes = dict(self.role_plan)[role]
            alternatives = [self._replace_role(course, role, option)
                            for option in self._role_options(role, stay_minutes, cursor)]
            if reason == "RETURN_TIME_EXCEEDED":
                # Try shorter optional replacements, then omission, before
                # changing required roles. Distance only orders attempts;
                # the routed timeline remains the authority on feasibility.
                alternatives.sort(key=lambda alternative: (
                    not any(stop["role"] == role for stop in alternative),
                    self._distance(alternative),
                ))
            for alternative in alternatives:
                keys = [get_place_identity(stop) for stop in alternative]
                identity = course_identity(alternative)
                if not alternative or len(keys) != len(set(keys)) or identity in self.visited:
                    continue
                if role in preferred:
                    urgent[identity] = alternative
                elif identity not in self.pending:
                    self.pending[identity] = alternative

        # Promote the failed role's remaining candidates, including candidates
        # already queued. Other roles remain available for changed arrival times.
        for identity in reversed(urgent):
            self.pending[identity] = urgent[identity]
            self.pending.move_to_end(identity, last=False)
