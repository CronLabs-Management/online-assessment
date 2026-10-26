from django.contrib import admin

from .models import (
    Application,
    AuditLog,
    CodingProblem,
    CodingTestCase,
    Exam,
    ExamSection,
    MCQOption,
    MCQQuestion,
)


class MCQOptionInline(admin.TabularInline):
    model = MCQOption
    extra = 0


class MCQQuestionInline(admin.TabularInline):
    model = MCQQuestion
    extra = 0
    show_change_link = True


class ExamSectionInline(admin.TabularInline):
    model = ExamSection
    extra = 0


class CodingTestCaseInline(admin.TabularInline):
    model = CodingTestCase
    extra = 0


@admin.register(Exam)
class ExamAdmin(admin.ModelAdmin):
    list_display = ("title", "opens_at", "closes_at", "is_published", "created_by")
    list_filter = ("is_published",)
    search_fields = ("title",)
    inlines = [ExamSectionInline, MCQQuestionInline]


@admin.register(MCQQuestion)
class MCQQuestionAdmin(admin.ModelAdmin):
    list_display = ("text", "exam", "marks", "order")
    list_filter = ("exam",)
    inlines = [MCQOptionInline]


@admin.register(CodingProblem)
class CodingProblemAdmin(admin.ModelAdmin):
    list_display = ("title", "exam", "marks", "order")
    list_filter = ("exam",)
    inlines = [CodingTestCaseInline]


@admin.register(Application)
class ApplicationAdmin(admin.ModelAdmin):
    list_display = ("full_name", "exam", "candidate", "status", "credentials_sent_at", "details_confirmed_at")
    list_filter = ("status", "exam")
    search_fields = ("full_name", "candidate__username", "candidate__email")
    readonly_fields = ("changed_by", "changed_at", "created_at", "credentials_sent_at", "details_confirmed_at")


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "actor", "action", "target")
    list_filter = ("action",)
    readonly_fields = ("actor", "action", "target", "detail", "created_at")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
