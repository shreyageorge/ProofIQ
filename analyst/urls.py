from django.urls import path
from . import views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("upload/", views.upload_files, name="upload"),
    path("ask/", views.ask_question, name="ask"),
    path("clear/", views.clear_files, name="clear"),
    path("delete/<int:file_index>/", views.delete_file, name="delete_file"),
]
