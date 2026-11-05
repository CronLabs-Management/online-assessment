from django.conf import settings
from django.core.checks import Error, Tags, register


@register(Tags.security, deploy=True)
def check_runner_security(app_configs, **kwargs):
    errors = []
    if not settings.DEBUG and getattr(settings, "CODE_RUNNER", "").strip().lower() == "local":
        errors.append(
            Error(
                "CODE_RUNNER=local cannot be used when DEBUG is False. Use Judge0 or an isolated sandbox runner in production.",
                hint="Set CODE_RUNNER=judge0 and provide JUDGE0_URL, or leave CODE_RUNNER empty with JUDGE0_URL set.",
                id="assessments.E001",
            )
        )
    return errors
