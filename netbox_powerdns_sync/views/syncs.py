from django.contrib import messages
from django.db.models import Q
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.views.generic import View
from core.models import Job, ObjectType
from utilities.rqworker import get_workers_for_queue
from utilities.querydict import normalize_querydict
from utilities.views import ContentTypePermissionRequiredMixin

from ..constants import JOB_NAME_DEVICE, JOB_NAME_INTERFACE, JOB_NAME_IP, JOB_NAME_SYNC
from ..jobs import PowerdnsTaskFullSync
from ..forms import ZoneScheduleForm
from ..models import Zone
from ..tables import SyncJobTable

__all__ = (
    "SyncJobsView",
    "SyncResultView",
    "SyncScheduleView",
)


class aaaSyncRunView(ContentTypePermissionRequiredMixin, View):

    def get_required_permission(self):
        return "extras.view_script"

    def get(self, request, pk):
        if not request.user.has_perm("extras.run_script"):
            return HttpResponseForbidden()

        zone = get_object_or_404(Zone.objects.restrict(request.user), pk=pk)
        if not zone.enabled:
            messages.error(request, f"Unable to sync disabled zone {zone}")
        elif not get_workers_for_queue("default"):
            messages.error(request, "Unable to run script: RQ worker process not running.")
        else:
            job = Job.enqueue(
                PowerdnsTaskFullSync.run_full_sync,
                instance=zone,
                name=JOB_NAME_SYNC,
                user=request.user,
            )
            return redirect("plugins:netbox_powerdns_sync:sync_result", job_pk=job.pk)

        return redirect(zone)


class SyncJobsView(ContentTypePermissionRequiredMixin, View):

    def get_required_permission(self):
        return "extras.view_script"

    def get(self, request):
        # Filter jobs by object_type OR by name pattern (for jobs without instance)
        query = Q(app_label="netbox_powerdns_sync", model="zone") | Q(app_label="ipam", model="ipaddress")
        object_types = ObjectType.objects.filter(query)

        # Jobs with object_type (legacy) OR jobs matching our name patterns
        # Include various sync job name patterns
        jobs = Job.objects.filter(
            Q(object_type__in=object_types) |
            Q(name__startswith=JOB_NAME_SYNC) |
            Q(name__icontains="Sync") |  # Catch "Test Sync", "E2E Bootstrap Sync", etc.
            Q(name__in=(JOB_NAME_DEVICE, JOB_NAME_INTERFACE, JOB_NAME_IP))
        ).order_by("-created")
        jobs_table = SyncJobTable(
            data=jobs,
            orderable=False,
            user=request.user
        )
        jobs_table.configure(request)

        return render(request, "netbox_powerdns_sync/syncs.html", {
            "table": jobs_table,
            "tab": "jobs",
        })

    def post(self, request):
        if not request.user.has_perm("core.delete_job"):
            return HttpResponseForbidden()

        if "delete" in request.POST:
            job_pk = request.POST.get("delete")
            job = get_object_or_404(Job.objects.all(), pk=job_pk)
            job.delete()
            messages.success(request, f"Job {job_pk} deleted successfully.")

        return redirect("plugins:netbox_powerdns_sync:sync_jobs")


class SyncResultView(ContentTypePermissionRequiredMixin, View):

    def get_required_permission(self):
        return "extras.view_script"

    def get(self, request, job_pk):
        job = get_object_or_404(Job.objects.all(), pk=job_pk)

        # Filter logs by level if requested
        log_level = request.GET.get("level")
        logs = job.data.get("log", []) if job.data else []
        
        if log_level:
            level_map = {
                "0": ["default", "debug", "info", "success", "warning", "failure"],  # Include "default" for backward compatibility
                "1": ["info", "success", "warning", "failure"],
                "2": ["success", "warning", "failure"],
                "3": ["warning", "failure"],
                "4": ["failure"],
            }
            allowed_statuses = level_map.get(log_level, [])
            if allowed_statuses:
                logs = [log for log in logs if log.get("status") in allowed_statuses]

        # If this is an HTMX request, return only the result HTML
        if request.htmx:
            response = render(request, "netbox_powerdns_sync/htmx/sync_result.html", {
                "job": job,
                "logs": logs,
                "selected_level": log_level or "0",
            })
            if job.completed or not job.started:
                response.status_code = 286
            return response

        return render(request, "netbox_powerdns_sync/sync_result.html", {
            "job": job,
            "logs": logs,
            "selected_level": log_level or "0",
        })


class SyncScheduleView(View):
    def get(self, request):
        scheduled_jobs = Job.objects.filter(status="scheduled", name=JOB_NAME_SYNC)
        jobs_table = SyncJobTable(
            data=scheduled_jobs,
            orderable=False,
            user=request.user
        )
        jobs_table.columns.show("scheduled")
        jobs_table.columns.show("interval")
        jobs_table.columns.show("object")
        jobs_table.configure(request)

        form = ZoneScheduleForm(initial=normalize_querydict(request.GET))

        return render(request, "netbox_powerdns_sync/sync_schedule.html", {
            "jobs_table": jobs_table,
            "form": form,
        })

    def post(self, request):
        form = ZoneScheduleForm(request.POST, request.FILES)
        
        if not get_workers_for_queue("default"):
            messages.error(request, "Unable to run script: RQ worker process not running.")
        elif form.is_valid():
            for zone in form.cleaned_data["zones"]:
                Job.enqueue(
                    PowerdnsTaskFullSync.run_full_sync,
                    instance=zone,
                    name=JOB_NAME_SYNC,
                    user=request.user,
                    schedule_at=form.cleaned_data.get("_schedule_at"),
                    interval=form.cleaned_data.get("_interval"),
                )
                messages.success(request, f"Scheduled sync job for zone {zone}")

        return redirect("plugins:netbox_powerdns_sync:sync_jobs")
