from django.urls import path

from . import api


urlpatterns = [
    path("auth/session/", api.session_api, name="api-session"),
    path("auth/login/", api.login_api, name="api-login"),
    path("auth/logout/", api.logout_api, name="api-logout"),
    path("profile/", api.profile_api, name="api-profile"),
    path("options/", api.options_api, name="api-options"),
    path("dashboard/", api.dashboard_api, name="api-dashboard"),
    path("transactions/", api.transactions_api, name="api-transactions"),
    path("transactions/<int:pk>/", api.transaction_api, name="api-transaction"),
    path("accounts/", api.accounts_api, name="api-accounts"),
    path("accounts/<int:pk>/", api.account_api, name="api-account"),
    path("contacts/", api.contacts_api, name="api-contacts"),
    path("contacts/<int:pk>/", api.contact_api, name="api-contact"),
    path("documents/", api.documents_api, name="api-documents"),
    path("documents/<int:pk>/", api.document_api, name="api-document"),
    path("documents/<int:pk>/payment/", api.payment_api, name="api-payment"),
    path("owners/", api.owners_api, name="api-owners"),
    path("reports/<slug:report>/", api.report_api, name="api-report"),
    path("periods/", api.periods_api, name="api-periods"),
    path("periods/<int:pk>/toggle/", api.period_toggle_api, name="api-period-toggle"),
    path("users/", api.users_api, name="api-users"),
    path("users/<int:pk>/", api.user_api, name="api-user"),
    path("audit/", api.audit_api, name="api-audit"),
]
