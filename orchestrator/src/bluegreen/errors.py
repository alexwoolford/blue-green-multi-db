"""Process exit codes from the spec."""


class AppError(Exception):
    code = 1

    def __init__(self, message: str, *, error_code: str | None = None, code: int | None = None):
        super().__init__(message)
        self.error_code = error_code
        if code is not None:
            self.code = code


class ConfigError(AppError):
    code = 2


class ApiError(AppError):
    code = 3


class VerificationError(AppError):
    code = 4


class OpenQuestionError(AppError):
    """An unresolved hypothesis blocks progress. The message names the hypothesis or OQ."""

    code = 5


class AdminError(AppError):
    code = 3

    def __init__(self, step: str, exit_code: int, log_path: str, tail: str):
        super().__init__(
            f"neo4j-admin {step} failed with exit {exit_code}. Log: {log_path}\n{tail}",
            error_code=str(exit_code),
        )
        self.step = step
        self.exit_code = exit_code
        self.log_path = log_path
