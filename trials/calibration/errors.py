class CalibrationError(Exception):
    exit_code = 1


class ConfigError(CalibrationError):
    exit_code = 2


class ApiError(CalibrationError):
    exit_code = 3


class ZeroMatchError(CalibrationError):
    exit_code = 4


class DataError(CalibrationError):
    exit_code = 5
