"""
Time Utilities for Weekly Flight Scheduling

Handles conversion between:
- Game time: 168-hour week (MON 00:00 to SUN 23:59)
- Session time: 1200 seconds (20 minutes of real time)

Time compression ratio: 1 session second = 5.04 game minutes
"""

import json


# Day name to index mapping
DAYS = {
    'MON': 0, 'TUE': 1, 'WED': 2, 'THU': 3,
    'FRI': 4, 'SAT': 5, 'SUN': 6
}
DAYS_LIST = ['MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT', 'SUN']


def parse_time(time_str):
    """
    Parse and validate time string in HH:MM format.
    
    Args:
        time_str: Time string like "08:00", "14:30", "22:15"
    
    Returns:
        tuple: (hour, minute) as integers
    
    Raises:
        ValueError: If format is invalid
    """
    if not time_str or ':' not in time_str:
        raise ValueError(f"Invalid time format: {time_str}. Expected HH:MM")
    
    try:
        parts = time_str.strip().split(':')
        if len(parts) != 2:
            raise ValueError("Time must be in HH:MM format")
        
        hour = int(parts[0])
        minute = int(parts[1])
        
        if not (0 <= hour <= 23):
            raise ValueError(f"Hour must be 0-23, got {hour}")
        if not (0 <= minute <= 59):
            raise ValueError(f"Minute must be 0-59, got {minute}")
        
        return (hour, minute)
    except ValueError as e:
        raise ValueError(f"Invalid time '{time_str}': {e}")


def format_time(hour, minute):
    """Format hour and minute as HH:MM string."""
    return f"{hour:02d}:{minute:02d}"


def parse_days(days_input):
    """
    Parse days of week input.
    
    Args:
        days_input: Either "DAILY" or comma-separated day numbers (1-7)
                   or comma-separated day names (MON,WED,FRI)
    
    Returns:
        list: List of day names like ['MON', 'WED', 'FRI']
    
    Examples:
        parse_days("DAILY") → ['MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT', 'SUN']
        parse_days("1,3,5") → ['MON', 'WED', 'FRI']
        parse_days("MON,WED,FRI") → ['MON', 'WED', 'FRI']
    """
    days_input = days_input.strip().upper()
    
    if days_input == "DAILY":
        return DAYS_LIST.copy()
    
    # Try parsing as numbers (1-7)
    if all(c.isdigit() or c == ',' or c.isspace() for c in days_input):
        try:
            numbers = [int(x.strip()) for x in days_input.split(',')]
            result = []
            for num in numbers:
                if not (1 <= num <= 7):
                    raise ValueError(f"Day number must be 1-7, got {num}")
                result.append(DAYS_LIST[num - 1])
            return result
        except ValueError as e:
            raise ValueError(f"Invalid day numbers: {e}")
    
    # Try parsing as day names
    day_names = [x.strip().upper() for x in days_input.split(',')]
    result = []
    for name in day_names:
        if name not in DAYS:
            raise ValueError(f"Invalid day name: {name}. Use MON, TUE, WED, THU, FRI, SAT, SUN")
        result.append(name)
    
    return result


def days_to_json(days_list):
    """Convert days list to JSON string for storage."""
    if days_list == DAYS_LIST:
        return "DAILY"
    return json.dumps(days_list)


def days_from_json(days_json):
    """Parse days from JSON string."""
    if days_json == "DAILY":
        return DAYS_LIST.copy()
    return json.loads(days_json)


def game_time_to_session_sec(day_of_week, time_str):
    """
    Convert game day+time to session seconds.
    
    Session time is for UI visualization only. The 1200-second session
    represents the 168-hour game week compressed.
    
    Args:
        day_of_week: 'MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT', 'SUN'
        time_str: Time in HH:MM format
    
    Returns:
        int: Session second (0-1199)
    
    Examples:
        game_time_to_session_sec('MON', '00:00') → 0
        game_time_to_session_sec('MON', '08:00') → 57
        game_time_to_session_sec('WED', '12:00') → 514
        game_time_to_session_sec('SUN', '23:59') → 1199
    
    Formula:
        game_hours = (day_index × 24) + hour + (minute / 60)
        session_sec = game_hours × (1200 / 168)
    """
    if day_of_week not in DAYS:
        raise ValueError(f"Invalid day: {day_of_week}")
    
    hour, minute = parse_time(time_str)
    day_index = DAYS[day_of_week]
    
    # Calculate total game hours from start of week
    game_hours = (day_index * 24) + hour + (minute / 60.0)
    
    # Convert to session seconds
    # 168 game hours = 1200 session seconds
    session_sec = int(game_hours * (1200.0 / 168.0))
    
    # Clamp to valid range
    return max(0, min(1199, session_sec))


def session_sec_to_game_time(session_sec):
    """
    Convert session seconds to game day+time.
    
    Args:
        session_sec: Session second (0-1199)
    
    Returns:
        tuple: (day_of_week, time_str)
    
    Examples:
        session_sec_to_game_time(0) → ('MON', '00:00')
        session_sec_to_game_time(57) → ('MON', '08:00')
        session_sec_to_game_time(514) → ('WED', '12:00')
        session_sec_to_game_time(1199) → ('SUN', '23:55')
    """
    # Convert session seconds to game hours
    game_hours = session_sec * (168.0 / 1200.0)
    
    # Calculate day and time
    day_index = int(game_hours // 24)
    hour = int(game_hours % 24)
    minute = int((game_hours % 1) * 60)
    
    # Clamp day to valid range
    day_index = max(0, min(6, day_index))
    
    return (DAYS_LIST[day_index], format_time(hour, minute))


def add_hours_to_time(day_of_week, time_str, hours_to_add):
    """
    Add hours to a game time, wrapping to next day if needed.
    
    Args:
        day_of_week: Starting day ('MON', 'TUE', etc.)
        time_str: Starting time ('HH:MM')
        hours_to_add: Hours to add (can have decimal for minutes)
    
    Returns:
        tuple: (new_day, new_time_str)
    
    Examples:
        add_hours_to_time('MON', '08:00', 4.5) → ('MON', '12:30')
        add_hours_to_time('MON', '22:00', 3) → ('TUE', '01:00')
        add_hours_to_time('SUN', '22:00', 3) → ('MON', '01:00')  # Wraps to next week
    """
    hour, minute = parse_time(time_str)
    day_index = DAYS[day_of_week]
    
    # Convert to total minutes from start of week
    total_minutes = (day_index * 24 * 60) + (hour * 60) + minute
    
    # Add the hours
    total_minutes += int(hours_to_add * 60)
    
    # Wrap around week (168 hours = 10080 minutes)
    total_minutes = total_minutes % (168 * 60)
    
    # Convert back to day and time
    new_day_index = total_minutes // (24 * 60)
    remaining_minutes = total_minutes % (24 * 60)
    new_hour = remaining_minutes // 60
    new_minute = remaining_minutes % 60
    
    return (DAYS_LIST[new_day_index], format_time(new_hour, new_minute))


def time_to_hours(time_str):
    """
    Convert HH:MM time to decimal hours.
    
    Args:
        time_str: Time string like "08:30"
    
    Returns:
        float: Hours as decimal (e.g., 8.5 for 8:30)
    """
    hour, minute = parse_time(time_str)
    return hour + (minute / 60.0)


def hours_to_time(hours):
    """
    Convert decimal hours to HH:MM time.
    
    Args:
        hours: Hours as decimal (e.g., 8.5 for 8:30)
    
    Returns:
        str: Time in HH:MM format
    """
    hour = int(hours)
    minute = int((hours % 1) * 60)
    return format_time(hour, minute)


def format_days_display(days_list):
    """
    Format days list for display.
    
    Args:
        days_list: List of day names
    
    Returns:
        str: Formatted string like "Daily" or "Mon, Wed, Fri"
    """
    if len(days_list) == 7 and days_list == DAYS_LIST:
        return "Daily"
    
    return ", ".join(days_list)


# Test the functions if run directly
if __name__ == "__main__":
    print("Time Utilities Test")
    print("=" * 60)
    
    # Test parse_time
    print("\n1. Parse Time:")
    print(f"  parse_time('08:00') = {parse_time('08:00')}")
    print(f"  parse_time('14:30') = {parse_time('14:30')}")
    
    # Test parse_days
    print("\n2. Parse Days:")
    print(f"  parse_days('DAILY') = {parse_days('DAILY')}")
    print(f"  parse_days('1,3,5') = {parse_days('1,3,5')}")
    print(f"  parse_days('MON,WED,FRI') = {parse_days('MON,WED,FRI')}")
    
    # Test game_time_to_session_sec
    print("\n3. Game Time → Session Seconds:")
    print(f"  MON 00:00 → {game_time_to_session_sec('MON', '00:00')}s")
    print(f"  MON 08:00 → {game_time_to_session_sec('MON', '08:00')}s")
    print(f"  WED 12:00 → {game_time_to_session_sec('WED', '12:00')}s")
    print(f"  SUN 23:59 → {game_time_to_session_sec('SUN', '23:59')}s")
    
    # Test session_sec_to_game_time
    print("\n4. Session Seconds → Game Time:")
    print(f"  0s → {session_sec_to_game_time(0)}")
    print(f"  57s → {session_sec_to_game_time(57)}")
    print(f"  514s → {session_sec_to_game_time(514)}")
    print(f"  1199s → {session_sec_to_game_time(1199)}")
    
    # Test add_hours_to_time
    print("\n5. Add Hours to Time:")
    print(f"  MON 08:00 + 4.5h = {add_hours_to_time('MON', '08:00', 4.5)}")
    print(f"  MON 22:00 + 3h = {add_hours_to_time('MON', '22:00', 3)}")
    print(f"  SUN 22:00 + 3h = {add_hours_to_time('SUN', '22:00', 3)}")
