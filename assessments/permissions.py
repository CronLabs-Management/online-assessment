"""Role-based access for the people who run tests.

Three kinds of user:

* **Admin**: a Django ``is_staff`` account. Admins can do everything, always.
* **Staff**: an interviewer or team member. What they can do is set by the role an admin gives them
  (``StaffRole``), and an admin can change those roles in the Team page without touching code.
* **Candidate**: everyone else. Candidates can only take the tests they were invited to.

Views check one permission each, through :func:`permission_required`.
"""

from functools import wraps

from django.conf import settings
from django.contrib.auth.views import redirect_to_login

# Each permission: code, short label, one-line description, and the area it belongs to.
# The codes are stored on roles, so never rename one without a data migration.
PERMISSIONS = [
    ("tests.view", "View tests", "See the list of tests, their settings and the results overview.", "Tests"),
    ("tests.manage", "Create and edit tests", "Create tests, change settings, sections and rounds, publish and unpublish.", "Tests"),
    ("questions.manage", "Manage questions", "Add, edit and delete MCQ and coding questions.", "Tests"),
    ("candidates.view", "View candidates", "See the candidate list and candidate details for a test.", "Candidates"),
    ("candidates.manage", "Manage candidates", "Import and edit candidates, remove or restore access, allow a resume.", "Candidates"),
    ("credentials.view", "View login passwords", "See candidates' login passwords.", "Candidates"),
    ("credentials.send", "Send login details", "Send, resend and regenerate candidates' login details.", "Candidates"),
    ("results.view", "View results and evidence", "Open attempts: answers, marks, insights and proctoring evidence.", "Results"),
    ("results.export", "Export results", "Download candidate and result data as CSV.", "Results"),
    ("interviews.conduct", "Conduct interviews", "Record interview scores, remarks and next-round decisions; generate AI questions.", "Interviews"),
    ("settings.manage", "Manage site settings", "Change branding, wording, email text and retention settings.", "Administration"),
    ("audit.view", "View audit log", "Read the audit log of staff actions.", "Administration"),
    ("team.manage", "Manage team and roles", "Add staff members and change what each role can do.", "Administration"),
]

PERMISSION_CODES = [code for code, *_ in PERMISSIONS]
PERMISSION_LABELS = {code: label for code, label, *_ in PERMISSIONS}

# Given to the role created for existing members of the old "Reviewers" group, and to anyone still in
# that group without a role.
INTERVIEWER_PERMISSIONS = ["tests.view", "candidates.view", "results.view", "interviews.conduct"]


def permission_groups():
    """Permissions grouped by area, in display order: [(area, [(code, label, description), ...]), ...]."""
    groups = {}
    for code, label, description, area in PERMISSIONS:
        groups.setdefault(area, []).append((code, label, description))
    return list(groups.items())


def is_admin(user):
    return bool(user and user.is_authenticated and user.is_active and (user.is_staff or user.is_superuser))


def _legacy_reviewer(user):
    group = getattr(settings, "REVIEWER_GROUP", "Reviewers")
    return user.groups.filter(name=group).exists()


def user_permissions(user):
    """The set of permission codes this user holds. Cached on the user object for the request."""
    if not (user and user.is_authenticated and user.is_active):
        return frozenset()
    cached = getattr(user, "_oa_permissions", None)
    if cached is not None:
        return cached
    if is_admin(user):
        perms = frozenset(PERMISSION_CODES)
    else:
        from .models import StaffProfile

        profile = StaffProfile.objects.filter(user=user, is_active=True).select_related("role").first()
        if profile is not None and profile.role is not None:
            perms = frozenset(code for code in profile.role.permissions if code in PERMISSION_LABELS)
        elif _legacy_reviewer(user):
            perms = frozenset(INTERVIEWER_PERMISSIONS)
        else:
            perms = frozenset()
    user._oa_permissions = perms
    return perms


def has_perm(user, code):
    return code in user_permissions(user)


def is_team_member(user):
    """Admins and staff. Team members never take tests and see the staff area after signing in."""
    if not (user and user.is_authenticated):
        return False
    if is_admin(user):
        return True
    from .models import StaffProfile

    if StaffProfile.objects.filter(user=user).exists():
        return True
    return _legacy_reviewer(user)


def role_label(user):
    if is_admin(user):
        return "Admin"
    from .models import StaffProfile

    profile = StaffProfile.objects.filter(user=user).select_related("role").first()
    if profile is not None:
        return profile.role.name if profile.role else "Staff"
    if _legacy_reviewer(user):
        return "Interviewer"
    return "Candidate"


def _guard(test):
    """Like Django's user_passes_test: anyone who fails the test is sent to sign in with a different account."""

    def decorator(view):
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            if not test(request.user):
                return redirect_to_login(request.get_full_path())
            return view(request, *args, **kwargs)

        return wrapper

    return decorator


def permission_required(*codes):
    """Require sign-in and every listed permission."""
    return _guard(lambda user: all(has_perm(user, code) for code in codes))


def any_permission_required(*codes):
    """Require sign-in and at least one of the listed permissions."""
    return _guard(lambda user: any(has_perm(user, code) for code in codes))


admin_required = _guard(is_admin)
team_required = _guard(is_team_member)
