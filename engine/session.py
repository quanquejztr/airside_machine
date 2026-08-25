"""
Backward compatibility: continuous game clock and game_state helpers live in ``engine.clock``.
"""

from engine.clock import (  # noqa: F401
    GameClock,
    can_schedule_flights,
    get_display_game_hours,
    get_game_time,
    get_global_clock,
    init_game_state,
    is_clock_running,
    start_game_clock,
    stop_game_clock,
)
