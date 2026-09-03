from django.urls import path

from . import spa, views


urlpatterns = [
    path("", spa.spa, name="dashboard"),
    path("login/", spa.spa, name="login"),
    path("transactions/", spa.spa, name="transaction-list"),
    path("transactions/new/", spa.spa, name="transaction-create"),
    path("transactions/new/split/", spa.spa, name="transaction-split-create"),
    path("transactions/<int:pk>/", spa.spa, name="transaction-detail"),
    path("transactions/<int:pk>/edit/", spa.spa, name="transaction-edit"),
    path("accounts/", spa.spa, name="account-list"),
    path("accounts/new/", spa.spa, name="account-create"),
    path("accounts/<int:pk>/", spa.spa, name="account-detail"),
    path("accounts/<int:pk>/edit/", spa.spa, name="account-edit"),
    path("contacts/", spa.spa, name="contact-list"),
    path("contacts/new/", spa.spa, name="contact-create"),
    path("contacts/<int:pk>/edit/", spa.spa, name="contact-edit"),
    path("documents/", spa.spa, name="document-list"),
    path("documents/new/<str:kind>/", spa.spa, name="document-create"),
    path("documents/<int:pk>/", spa.spa, name="document-detail"),
    path("documents/<int:pk>/payment/", spa.spa, name="payment-create"),
    path("owners/", spa.spa, name="owner-list"),
    path("owners/activity/new/", spa.spa, name="owner-activity-create"),
    path("reports/", spa.spa, name="reports"),
    path("reports/<slug:report>/", spa.spa, name="report-view"),
    path("settings/periods/", spa.spa, name="period-list"),
    path("settings/profile/", spa.spa, name="profile-settings"),
    path("settings/users/", spa.spa, name="user-list"),
    path("settings/users/<int:pk>/edit/", spa.spa, name="user-edit"),
    path("audit/", spa.spa, name="audit-list"),
    path("attachments/<int:pk>/download/", views.attachment_download, name="attachment-download"),
    path("export/excel/", views.export_workbook, name="export-workbook"),
]
