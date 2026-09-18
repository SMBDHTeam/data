"""Bounded, failure-directed course alternatives; no provider calls here."""

from collections import OrderedDict
from datetime import datetime, timedelta

from spontaneous.course import (
    ROLE_MAX_REQUIRED_STOPS,
    build_course_stop,
    calculate_distance_meters,
    can_cover_required_themes_for_role,
    get_place_identity,
    get_place_themes,
    get_required_themes_by_role,
    estimate_visit_at,
    limit_optional_course_stops,
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
    start_at=None,
    transport_mode=None,
    role_plan=None,
) -> dict[str, list[dict]]:
    """Rank within required coverage, estimating role times without routing I/O."""
    required = get_required_themes_by_role(desired_themes)
    role_times = {}
    cursor_time = start_at
    for role, stay_minutes in role_plan or []:
        role_times[role] = cursor_time
        if cursor_time is not None:
            cursor_time += timedelta(minutes=stay_minutes)
    ranked = {}
    for role, places in grouped_places.items():
        seen = set()
        candidates = []
        for place in sorted(places, key=lambda item: place_ranking_key(
            item, desired_themes, role, start_location,
            remaining_themes=required.get(role),
            departure_at=role_times.get(role, start_at), transport_mode=transport_mode,
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
                    departure_at=role_times.get(role, start_at), transport_mode=transport_mode,
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
                 start_location, role_plan, start_at=None, transport_mode=None, max_stops=None):
        self.candidates = ranked_candidates
        self.desired_themes = desired_themes
        self.start_location = start_location
        self.role_plan = role_plan
        self.start_at = start_at
        self.transport_mode = transport_mode
        self.max_stops = max_stops
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

    def _role_options(self, role, stay_minutes, cursor, departure_at=None, visit_at=None):
        candidates = self.candidates.get(role, [])
        required_themes = self.required.get(role, set())
        if not required_themes:
            options = [[build_course_stop(
                place, 1, role, stay_minutes, self.desired_themes, cursor, False,
            )] for place in sorted(candidates, key=lambda item: place_ranking_key(
                item, self.desired_themes, role, cursor,
                departure_at=departure_at, transport_mode=self.transport_mode, visit_at=visit_at,
            ))]
            # Exhaust same-role replacements before dropping an optional role.
            return options + [[]]

        options = []
        max_stops = ROLE_MAX_REQUIRED_STOPS.get(role) or len(candidates)

        def cover(selected, remaining, location, used, cursor_time):
            if not remaining:
                options.append(selected)
                return
            if len(selected) >= max_stops:
                return
            for place in sorted(candidates, key=lambda item: place_ranking_key(
                item, self.desired_themes, role, location, remaining_themes=remaining,
                departure_at=cursor_time, transport_mode=self.transport_mode,
                visit_at=visit_at if not selected else None,
            )):
                identity = get_place_identity(place)
                coverage = remaining & get_place_themes(place)
                if identity in used or not coverage:
                    continue
                stop = build_course_stop(
                    place, len(selected) + 1, role, stay_minutes,
                    self.desired_themes, location, True, coverage,
                )
                arrival_at = (visit_at if not selected and visit_at is not None else
                              estimate_visit_at(place, location, cursor_time, self.transport_mode))
                next_time = arrival_at + timedelta(minutes=stay_minutes) if arrival_at is not None else None
                cover(selected + [stop], remaining - coverage, stop, used | {identity}, next_time)

        # At most five candidates: <= 325 ordered subsets, without network I/O.
        # Each additional ACTIVITY must cover a previously uncovered theme.
        cover([], required_themes, cursor, set(), departure_at)
        return options

    def _replace_role(self, course, role, stops):
        blocks = {planned_role: [] for planned_role, _ in self.role_plan}
        for stop in course:
            blocks[stop["role"]].append(stop)
        blocks[role] = stops
        return limit_optional_course_stops(normalize_course_orders([
            stop for planned_role, _ in self.role_plan for stop in blocks[planned_role]
        ]), self.max_stops)

    def _distance(self, course):
        locations = [self.start_location, *course, self.start_location]
        return sum(calculate_distance_meters(origin, destination)
                   for origin, destination in zip(locations, locations[1:]))

    def retry(self, course: list[dict], reason: str, failed_index: int | None = None,
              timeline: list[dict] | None = None) -> None:
        roles = [role for role, _ in self.role_plan]
        role_order = {role: index for index, role in enumerate(roles)}
        present_roles = {stop["role"] for stop in course}
        if failed_index is not None:
            preferred = [course[failed_index]["role"]]
            # An unroutable edge can also be fixed by replacing its origin.
            if reason == "NO_ROUTE" and failed_index > 0:
                origin_role = course[failed_index - 1]["role"]
                roles = [origin_role] + [role for role in roles if role != origin_role]
        elif reason in {"RETURN_TIME_EXCEEDED", "STOP_LIMIT_EXCEEDED"}:
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
            cursor_time = self.start_at
            # Times are local to this failed attempt, never cached by role across
            # changed paths. Replacement candidates use its actual visit window
            # for ordering; every alternative is still routed and checked again.
            timed_stops = {get_place_identity(stop): stop for stop in timeline or []}
            visit_at = None
            for stop in course:
                timed = timed_stops.get(get_place_identity(stop))
                if stop["role"] == role:
                    if timed:
                        visit_at = datetime.fromisoformat(timed["arrivalAt"])
                    break
                if role_order[stop["role"]] < role_order[role]:
                    if timed:
                        cursor_time = datetime.fromisoformat(timed["departureAt"])
                    elif cursor_time is not None:
                        cursor_time = estimate_visit_at(
                            stop, cursor, cursor_time, self.transport_mode,
                        ) + timedelta(minutes=stop["stayMinutes"])
                    cursor = stop
            stay_minutes = dict(self.role_plan)[role]
            alternatives = [self._replace_role(course, role, option)
                            for option in self._role_options(role, stay_minutes, cursor, cursor_time, visit_at)]
            if reason in {"RETURN_TIME_EXCEEDED", "STOP_LIMIT_EXCEEDED"}:
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
