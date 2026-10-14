from django.contrib import admin

from .models import (
    Account,
    AccountingPeriod,
    ApiToken,
    Attachment,
    AuditEvent,
    BusinessDocument,
    Contact,
    JournalEntry,
    JournalLine,
    OwnerActivity,
    Payment,
    UserProfile,
)


class JournalLineInline(admin.TabularInline):
    model = JournalLine
    extra = 0


@admin.register(JournalEntry)
class JournalEntryAdmin(admin.ModelAdmin):
    list_display = ["number", "date", "description", "source", "status", "version"]
    list_filter = ["status", "source", "date"]
    search_fields = ["number", "description", "memo"]
    inlines = [JournalLineInline]


@admin.register(ApiToken)
class ApiTokenAdmin(admin.ModelAdmin):
    list_display = ["name", "user", "hint", "created_at", "last_used_at"]
    readonly_fields = ["token_hash", "hint", "created_at", "last_used_at"]


for model in [Account, AccountingPeriod, Attachment, AuditEvent, BusinessDocument, Contact, OwnerActivity, Payment, UserProfile]:
    admin.site.register(model)
