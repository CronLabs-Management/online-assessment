"""Team page: staff members and the roles that decide what they can do. Needs the team.manage permission."""

from django.contrib import messages
from django.db import transaction
from django.db.models import Count
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from . import notifications
from .forms import StaffMemberForm, StaffRoleForm
from .models import SiteSettings, StaffProfile, StaffRole
from .permissions import PERMISSION_LABELS, permission_groups, permission_required
from .views import User, audit


def _split_name(full_name):
    parts = (full_name or "").strip().split(" ", 1)
    return parts[0], (parts[1] if len(parts) > 1 else "")


def _send_staff_login(user, password, actor):
    site = SiteSettings.load().site_name or "Assessments"
    login_link = notifications._absolute(reverse("login"))
    body = (
        f"Hello {user.first_name or user.email},\n\n"
        f"You have been added to the {site} team.\n\n"
        f"  Sign-in page: {login_link}\n"
        f"  Email:        {user.email}\n"
        f"  Password:     {password}\n\n"
        "Keep this email private.\n"
    )
    sent = notifications.deliver_email(f"Your {site} team login", body, user.email)
    audit(actor, "team.login_sent" if sent else "team.login_send_failed", f"user:{user.pk}", user.email)
    return sent


@permission_required("team.manage")
def team_overview(request):
    members = StaffProfile.objects.select_related("user", "role").order_by("-is_active", "user__email")
    roles = StaffRole.objects.annotate(member_count=Count("members")).order_by("name")
    admins = User.objects.filter(is_active=True).filter(is_staff=True).order_by("email")
    role_rows = [
        {"role": role, "labels": [PERMISSION_LABELS[c] for c in role.permissions if c in PERMISSION_LABELS]}
        for role in roles
    ]
    return render(
        request,
        "assessments/staff/team.html",
        {"members": members, "role_rows": role_rows, "admins": admins},
    )


@permission_required("team.manage")
def team_member_add(request):
    if not StaffRole.objects.exists():
        messages.info(request, "Create a role first, then add people to it.")
        return redirect("team_role_add")
    form = StaffMemberForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        first, last = _split_name(data["full_name"])
        password = notifications.new_password()
        with transaction.atomic():
            user = User.objects.filter(email__iexact=data["email"]).first() or User(
                username=data["email"], email=data["email"]
            )
            user.first_name, user.last_name = first[:150], last[:150]
            user.is_active = True
            user.set_password(password)
            user.save()
            StaffProfile.objects.create(
                user=user, role=data["role"], title=data["title"], is_active=data["is_active"]
            )
        audit(request.user, "team.member_added", f"user:{user.pk}", f"{user.email} as {data['role'].name}")
        sent = _send_staff_login(user, password, request.user)
        note = "Their login was emailed to them." if sent else "The email could not be sent."
        messages.success(request, f"{user.email} added as {data['role'].name}. {note} Password: {password}")
        return redirect("team_overview")
    return render(request, "assessments/staff/team_member_form.html", {"form": form, "profile": None})


@permission_required("team.manage")
def team_member_edit(request, pk):
    profile = get_object_or_404(StaffProfile.objects.select_related("user", "role"), pk=pk)
    form = StaffMemberForm(request.POST or None, profile=profile)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        user = profile.user
        user.first_name, user.last_name = (part[:150] for part in _split_name(data["full_name"]))
        user.save(update_fields=["first_name", "last_name"])
        before = (profile.role_id, profile.is_active)
        profile.role = data["role"]
        profile.title = data["title"]
        profile.is_active = data["is_active"]
        profile.save()
        detail = f"role={profile.role.name}, active={profile.is_active}"
        if before != (profile.role_id, profile.is_active):
            audit(request.user, "team.member_changed", f"user:{user.pk}", detail)
        messages.success(request, f"Saved {user.email}.")
        return redirect("team_overview")
    return render(request, "assessments/staff/team_member_form.html", {"form": form, "profile": profile})


@permission_required("team.manage")
@require_POST
def team_member_reset_password(request, pk):
    profile = get_object_or_404(StaffProfile.objects.select_related("user"), pk=pk)
    password = notifications.new_password()
    profile.user.set_password(password)
    profile.user.save(update_fields=["password"])
    audit(request.user, "team.password_reset", f"user:{profile.user_id}", profile.user.email)
    sent = _send_staff_login(profile.user, password, request.user)
    note = "The new login was emailed." if sent else "The email could not be sent."
    messages.success(request, f"New password for {profile.user.email}: {password}. {note}")
    return redirect("team_overview")


@permission_required("team.manage")
def team_role_add(request):
    return _role_form(request, StaffRole())


@permission_required("team.manage")
def team_role_edit(request, pk):
    return _role_form(request, get_object_or_404(StaffRole, pk=pk))


def _role_form(request, role):
    form = StaffRoleForm(request.POST or None, instance=role)
    if request.method == "POST" and form.is_valid():
        created = role.pk is None
        role = form.save()
        audit(
            request.user,
            "team.role_created" if created else "team.role_changed",
            f"role:{role.pk}",
            f"{role.name}: {', '.join(role.permissions) or 'no permissions'}",
        )
        messages.success(request, f"Role \"{role.name}\" saved.")
        return redirect("team_overview")
    chosen = set(form["permissions"].value() or [])
    return render(
        request,
        "assessments/staff/team_role_form.html",
        {"form": form, "role": role, "groups": permission_groups(), "chosen": chosen},
    )


@permission_required("team.manage")
@require_POST
def team_role_delete(request, pk):
    role = get_object_or_404(StaffRole, pk=pk)
    if role.members.exists():
        messages.error(request, f"Move the people in \"{role.name}\" to another role before deleting it.")
        return redirect("team_overview")
    audit(request.user, "team.role_deleted", f"role:{role.pk}", role.name)
    role.delete()
    messages.success(request, "Role deleted.")
    return redirect("team_overview")
