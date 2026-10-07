import shutil
import tempfile
import uuid
from pathlib import Path

from django.conf import settings
from django.contrib import messages
from django.core.files.storage import FileSystemStorage
from django.http import HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.http import require_POST

from .services import ALLOWED_SUFFIXES, create_plan, execute_plan, load_tables, ollama_status, profile_tables, verify_result


def _records(request):
    return request.session.get("uploaded_files", [])


def _workspace_id(request):
    """Return a filesystem-safe ID that also works with signed-cookie sessions."""
    workspace_id = request.session.get("workspace_id")
    if not workspace_id:
        workspace_id = uuid.uuid4().hex
        request.session["workspace_id"] = workspace_id
    return workspace_id


def _is_managed_upload(path):
    upload_root = (Path(settings.MEDIA_ROOT) / "uploads").resolve()
    resolved = Path(path).resolve()
    return resolved != upload_root and upload_root in resolved.parents


def dashboard(request):
    records = _records(request)
    profile = {}
    available_records = [record for record in records if Path(record["path"]).is_file()]
    if available_records:
        try:
            tables, sources = load_tables(available_records)
            profile = profile_tables(tables, sources)
        except Exception as exc:
            messages.error(request, f"Could not inspect a dataset: {exc}")
    return render(request, "analyst/dashboard.html", {"files": records, "profile": profile, "ollama": ollama_status()})


@require_POST
def upload_files(request):
    uploads = request.FILES.getlist("datasets")
    if not uploads:
        messages.error(request, "Choose at least one CSV or Excel file.")
        return HttpResponseRedirect(reverse("dashboard"))
    folder = Path(settings.MEDIA_ROOT) / "uploads" / _workspace_id(request)
    folder.mkdir(parents=True, exist_ok=True)
    storage = FileSystemStorage(location=folder)
    # Vercel's /tmp filesystem is scoped to one function invocation. Replace
    # stale metadata there; the browser retains the real File objects and sends
    # them again with every analysis request.
    records = [] if settings.IS_VERCEL else _records(request)
    for upload in uploads:
        suffix = Path(upload.name).suffix.lower()
        if suffix not in ALLOWED_SUFFIXES:
            messages.warning(request, f"Skipped {upload.name}: use CSV or Excel.")
            continue
        if upload.size > 15 * 1024 * 1024:
            messages.warning(request, f"Skipped {upload.name}: 15 MB limit.")
            continue
        stored = storage.save(f"{uuid.uuid4().hex}{suffix}", upload)
        records.append({"name": Path(upload.name).name, "path": str(folder / stored), "size": upload.size})
    request.session["uploaded_files"] = records
    messages.success(request, f"Loaded {len(records)} dataset file(s).")
    if settings.IS_VERCEL:
        return dashboard(request)
    return HttpResponseRedirect(reverse("dashboard"))


@require_POST
def ask_question(request):
    question = request.POST.get("question", "").strip()
    records = _records(request)
    context = {"files": records, "question": question, "ollama": ollama_status()}
    request_uploads = request.FILES.getlist("datasets")
    if not question or (not records and not request_uploads):
        messages.error(request, "Upload data and enter a question first.")
        return HttpResponseRedirect(reverse("dashboard"))
    temporary_directory = None
    try:
        analysis_records = records
        if request_uploads:
            temporary_directory = tempfile.TemporaryDirectory(prefix="proofiq-analysis-")
            analysis_records = []
            for upload in request_uploads:
                suffix = Path(upload.name).suffix.lower()
                if suffix not in ALLOWED_SUFFIXES:
                    raise ValueError(f"Unsupported file type: {upload.name}")
                if upload.size > 15 * 1024 * 1024:
                    raise ValueError(f"{upload.name} exceeds the 15 MB limit.")
                path = Path(temporary_directory.name) / f"{uuid.uuid4().hex}{suffix}"
                with path.open("wb") as destination:
                    for chunk in upload.chunks():
                        destination.write(chunk)
                analysis_records.append({"name": Path(upload.name).name, "path": str(path), "size": upload.size})
        tables, sources = load_tables(analysis_records)
        profile = profile_tables(tables, sources)
        context["profile"] = profile
        plan = create_plan(question, profile)
        context["plan"] = plan
        if plan["status"] == "CANNOT_DETERMINE":
            context["cannot_determine"] = True
        else:
            try:
                execution = execute_plan(plan["code"], tables)
            except (RuntimeError, ValueError, SyntaxError) as first_error:
                plan = create_plan(
                    question,
                    profile,
                    execution_error=str(first_error),
                )
                context["plan"] = plan
                if plan["status"] == "CANNOT_DETERMINE":
                    context["cannot_determine"] = True
                    return render(request, "analyst/dashboard.html", context)
                execution = execute_plan(plan["code"], tables)
            context["execution"] = execution
            context["checks"] = verify_result(execution)
    except Exception as exc:
        context["analysis_error"] = str(exc)
    finally:
        if temporary_directory is not None:
            temporary_directory.cleanup()
    return render(request, "analyst/dashboard.html", context)


@require_POST
def clear_files(request):
    records = _records(request)
    failed = False
    for record in records:
        path = Path(record["path"])
        if _is_managed_upload(path) and path.is_file():
            try:
                path.unlink()
            except OSError:
                failed = True
    request.session["uploaded_files"] = []
    if failed:
        messages.warning(
            request,
            "Workspace cleared, but one or more stored files could not be deleted.",
        )
    else:
        messages.info(request, "Workspace cleared.")
    return HttpResponseRedirect(reverse("dashboard"))


@require_POST
def delete_file(request, file_index):
    records = list(_records(request))
    if file_index >= len(records):
        messages.error(request, "That uploaded file is no longer in this workspace.")
        return HttpResponseRedirect(reverse("dashboard"))

    record = records[file_index]
    path = Path(record["path"])
    if not _is_managed_upload(path):
        messages.error(request, "This file cannot be removed from the workspace.")
        return HttpResponseRedirect(reverse("dashboard"))

    try:
        if path.is_file():
            path.unlink()
    except OSError:
        messages.error(request, "The uploaded file could not be removed. Please retry.")
        return HttpResponseRedirect(reverse("dashboard"))

    del records[file_index]
    request.session["uploaded_files"] = records
    messages.success(request, f"Removed {record['name']} from the workspace.")
    return HttpResponseRedirect(reverse("dashboard"))
