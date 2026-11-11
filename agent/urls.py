from django.urls import path

from . import views

urlpatterns = [
    path("", views.index, name="index"),
    path("api/state", views.state, name="state"),
    path("api/chat", views.chat, name="chat"),
    path("api/actions/<int:pk>/<str:decision>", views.decide, name="decide"),
    path("api/reset", views.reset, name="reset"),
    path("healthz", views.healthz, name="healthz"),
]
