from __future__ import annotations


class ApiError(Exception):
    def __init__(self, code: str, message: str, status: int, **extra: object) -> None:
        super().__init__(message)
        self.code, self.message, self.status, self.extra = code, message, status, extra


def validation(msg: str) -> ApiError:
    return ApiError("validation_error", msg, 400)


def not_found(what: str = "not found") -> ApiError:
    return ApiError("not_found", what, 404)
