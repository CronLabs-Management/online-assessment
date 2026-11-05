from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path

from assessments.views import CandidateLoginView, favicon_view, logout_view, robots_txt_view

urlpatterns = [
    path("favicon.ico", favicon_view, name="favicon"),
    path("robots.txt", robots_txt_view, name="robots_txt"),
    path("admin/", admin.site.urls),
    path("login/", CandidateLoginView.as_view(), name="login_alias"),
    path("accounts/login/", CandidateLoginView.as_view(), name="login"),
    path("logout/", logout_view, name="logout_alias"),
    path("accounts/logout/", logout_view, name="logout"),
    path("", include("assessments.urls")),
]
