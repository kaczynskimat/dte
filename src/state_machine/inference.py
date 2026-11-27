IDLE_SPEED_THRESHOLD_KMH = 1.0
STEERING_THRESHOLD_RAD = 0.05
POWER_THRESHOLD_KW = 0.5
BRAKE_PRESSURE_THRESHOLD_BAR = 0.1


def _value(telemetry: dict, *keys: str, default=None):
    for key in keys:
        value = telemetry.get(key)
        if value is not None:
            return value
    return default


def _number(telemetry: dict, *keys: str, default: float = 0.0) -> float:
    try:
        return float(_value(telemetry, *keys, default=default))
    except (TypeError, ValueError):
        return default


def _flag(telemetry: dict, *keys: str) -> bool:
    value = _value(telemetry, *keys, default=False)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _speed_kmh(telemetry: dict) -> float:
    if _value(telemetry, "speed_kmh") is not None:
        return _number(telemetry, "speed_kmh")
    return _number(telemetry, "odometry_vehicleSpeed") * 3.6


def _power_kw(telemetry: dict) -> float:
    if _value(telemetry, "power_kw") is not None:
        return _number(telemetry, "power_kw")
    return _number(telemetry, "electric_powerDemand") / 1000.0


def _brake_bar(telemetry: dict) -> float:
    if _value(telemetry, "brake_bar") is not None:
        return _number(telemetry, "brake_bar")
    return _number(telemetry, "traction_brakePressure") / 100000.0


def _motion(speed_kmh: float) -> str:
    if speed_kmh > IDLE_SPEED_THRESHOLD_KMH:
        return "Forward"
    if speed_kmh < -IDLE_SPEED_THRESHOLD_KMH:
        return "Reverse"
    return "Stopped"


def _direction(telemetry: dict, motion: str) -> str:
    if motion == "Stopped":
        return "Stationary"

    steering_angle = _number(
        telemetry,
        "steering_angle_rad",
        "odometry_steeringAngle",
    )
    if steering_angle > STEERING_THRESHOLD_RAD:
        return "TurningLeft"
    if steering_angle < -STEERING_THRESHOLD_RAD:
        return "TurningRight"
    return "Straight"


def _energy(power_kw: float) -> str:
    if power_kw < -POWER_THRESHOLD_KW:
        return "Regenerating"
    if power_kw > POWER_THRESHOLD_KW:
        return "Consuming"
    return "Neutral"


def _braking(telemetry: dict) -> str:
    if _flag(telemetry, "park_brake", "status_parkBrakeIsActive"):
        return "ParkBrake"
    if _flag(telemetry, "halt_brake", "status_haltBrakeIsActive"):
        return "HaltBrake"
    if _brake_bar(telemetry) > BRAKE_PRESSURE_THRESHOLD_BAR:
        return "Braking"
    return "Released"


def _occupancy(telemetry: dict) -> str:
    passengers = int(_number(
        telemetry,
        "passengers",
        "itcs_numberOfPassengers",
    ))
    if passengers <= 0:
        return "Empty"
    if passengers <= 15:
        return "Low"
    if passengers <= 30:
        return "Medium"
    return "High"


def _heading(telemetry: dict) -> str:
    heading_deg = _number(telemetry, "heading_deg") % 360
    directions = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")
    return directions[int((heading_deg + 22.5) // 45) % 8]


def _was_at_stop(previous_state: str | None) -> bool:
    if not previous_state:
        return False
    parts = [part.strip() for part in previous_state.split("|")]
    return len(parts) > 7 and parts[7] == "AtStop"


def _stop_status(
    doors_open: bool,
    motion: str,
    previous_state: str | None,
) -> str:
    # Stop names are event-based and can be emitted after the bus has already
    # departed. Door activity is the reliable evidence that a stop is active.
    if doors_open:
        return "AtStop"
    if motion != "Stopped":
        return "BetweenStops"
    if _was_at_stop(previous_state):
        return "AtStop"
    return "BetweenStops"


def _transition_dimensions(state: str) -> str:
    parts = [part.strip() for part in state.split("|")]
    if len(parts) <= 7:
        return state.strip()
    return f"{parts[0]}/{parts[1]}/{parts[7]}"


def infer_transition(previous_state: str | None, current_state: str) -> str:
    """Describe a meaningful operation/motion/stop transition."""
    if not previous_state:
        return "Initialized"
    previous = _transition_dimensions(previous_state)
    current = _transition_dimensions(current_state)
    if previous == current:
        return "Unchanged"
    return f"{previous} -> {current}"


def infer_state(telemetry: dict, previous_state: str | None = None) -> str:
    speed_kmh = _speed_kmh(telemetry)
    power_kw = _power_kw(telemetry)
    motion = _motion(speed_kmh)
    doors_open = _flag(telemetry, "doors_open", "status_doorIsOpen")
    route = str(_value(telemetry, "itcs_busRoute", default="-")).strip()

    if doors_open:
        operation = "Boarding"
    elif power_kw < -POWER_THRESHOLD_KW:
        operation = "Regenerating"
    elif abs(speed_kmh) > IDLE_SPEED_THRESHOLD_KMH:
        operation = "Cruising"
    else:
        operation = "Idling"

    state_parts = (
        operation,
        motion,
        _direction(telemetry, motion),
        _braking(telemetry),
        _energy(power_kw),
        "Open" if doors_open else "Closed",
        "Available" if _flag(
            telemetry,
            "grid_available",
            "status_gridIsAvailable",
        ) else "Unavailable",
        _stop_status(doors_open, motion, previous_state),
        "InService" if route not in {"", "-"} else "Unknown",
        _occupancy(telemetry),
        _heading(telemetry),
    )
    return " | ".join(state_parts)
