from collections.abc import Sequence, Set

from race.race_state import RaceEndMode, RaceState

LeaderboardRow = tuple[int, int, int, float, float, float]


def resolve_post_lap_state(
    *,
    current_state: RaceState,
    race_end_mode: RaceEndMode,
    total_laps: int,
    leaderboard: Sequence[LeaderboardRow],
    active_contestants: Set[int],
) -> RaceState:
    if not leaderboard:
        return current_state

    next_state = current_state
    _leader_position, _leader_id, leader_laps, _best, _last, _total = leaderboard[0]

    if leader_laps >= total_laps and current_state == RaceState.RUNNING:
        if race_end_mode == RaceEndMode.WINNER:
            return RaceState.FINISHED
        if race_end_mode == RaceEndMode.LAST_CAR:
            next_state = RaceState.WINNER_DECLARED

    if (
        race_end_mode == RaceEndMode.LAST_CAR
        and active_contestants
        and next_state in (RaceState.RUNNING, RaceState.WINNER_DECLARED)
    ):
        # Only racers who have actually put up at least one lap in this race
        # count towards "everyone finished". Contestants carried over from a
        # previous race (or otherwise registered) who never complete a single
        # lap must not block the race from auto-ending.
        lap_counts_of_lapping_racers = [
            lap_count
            for _position, racer_id, lap_count, _best, _last, _total in leaderboard
            if racer_id in active_contestants and lap_count > 0
        ]
        if lap_counts_of_lapping_racers and all(
            lap_count >= total_laps for lap_count in lap_counts_of_lapping_racers
        ):
            return RaceState.FINISHED

    return next_state
