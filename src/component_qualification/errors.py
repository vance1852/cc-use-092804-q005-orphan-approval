"""部件质量服务向 API 和命令行暴露的稳定错误。"""


class ComponentError(RuntimeError):
    code = "component_error"
    status = 400


class NotFound(ComponentError):
    code = "not_found"
    status = 404


class Conflict(ComponentError):
    code = "conflict"
    status = 409


class Forbidden(ComponentError):
    code = "forbidden"
    status = 403


class InvalidState(ComponentError):
    code = "invalid_state"
    status = 409


class ValidationFailed(ComponentError):
    code = "validation_failed"
    status = 422
