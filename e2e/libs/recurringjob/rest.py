import time

from datetime import datetime

from kubernetes import client

from recurringjob.base import Base

from utility.constant import LONGHORN_NAMESPACE
from utility.utility import filter_cr
from utility.utility import get_longhorn_client
from utility.utility import logging
from utility.utility import get_retry_count_and_interval


class Rest(Base):

    def __init__(self):
        self.batch_v1_api = client.BatchV1Api()
        self.core_v1_api = client.CoreV1Api()
        self.retry_count, self.retry_interval = get_retry_count_and_interval()

    def create(self, name, task, groups, cron, retain, concurrency, labels, parameters):
        get_longhorn_client().create_recurring_job(
            Name=name,
            Task=task,
            Groups=groups,
            Cron=cron,
            Retain=retain,
            Concurrency=concurrency,
            Labels=labels,
            Parameters=parameters)
        self._wait_for_cron_job_create(name)

    def delete(self, job_name, volume_name):
        get_longhorn_client().delete(self.get(job_name))
        self._wait_for_cron_job_delete(job_name)
        self._wait_for_volume_recurringjob_delete(job_name, volume_name)

    def get(self, name):
        return get_longhorn_client().by_id_recurring_job(name)

    def add_to_volume(self, job_name, volume_name):
        volume = get_longhorn_client().by_id_volume(volume_name)
        volume.recurringJobAdd(name=job_name, isGroup=False)
        self._wait_for_volume_recurringjob_update(job_name, volume_name)

    def _wait_for_volume_recurringjob_update(self, job_name, volume_name):
        updated = False
        for _ in range(self.retry_count):
            jobs, _ = self.get_volume_recurringjobs_and_groups(volume_name)
            if job_name in jobs:
                updated = True
                break
            time.sleep(self.retry_interval)
        assert updated

    def _wait_for_volume_recurringjob_delete(self, job_name, volume_name):
        deleted = False
        for _ in range(self.retry_count):
            jobs, _ = self.get_volume_recurringjobs_and_groups(volume_name)
            if job_name not in jobs:
                deleted = True
                break
            time.sleep(self.retry_interval)
        assert deleted

    def get_volume_recurringjobs_and_groups(self, volume_name):
        for _ in range(self.retry_count):
            volume = None
            try:
                volume = get_longhorn_client().by_id_volume(volume_name)
                list = volume.recurringJobList()
                jobs = []
                groups = []
                for item in list:
                    if item['isGroup']:
                        groups.append(item['name'])
                    else:
                        jobs.append(item['name'])
                return jobs, groups
            except Exception as e:
                logging(f"Getting volume {volume} recurringjob list error: {e}")
                time.sleep(self.retry_interval)

    def _wait_for_cron_job_create(self, job_name):
        created = False
        for _ in range(self.retry_count):
            job = self.batch_v1_api.list_namespaced_cron_job(
                'longhorn-system',
                label_selector=f"recurring-job.longhorn.io={job_name}")
            if len(job.items) != 0:
                created = True
                break
            time.sleep(self.retry_interval)
        assert created

    def _wait_for_cron_job_delete(self, job_name):
        deleted = False
        for _ in range(self.retry_count):
            job = self.batch_v1_api.list_namespaced_cron_job(
                'longhorn-system',
                label_selector=f"recurring-job.longhorn.io={job_name}")
            if len(job.items) == 0:
                deleted = True
                break
            time.sleep(self.retry_interval)
        assert deleted

    def check_jobs_work(self, volume_name):
        jobs, _ = self.get_volume_recurringjobs_and_groups(volume_name)
        for job_name in jobs:
            job = self.get(job_name)
            logging(f"Checking recurringjob {job}")
            if job['task'] == 'snapshot':
                self._check_snapshot_created(volume_name, job_name)
            elif job['task'] == 'backup':
                self._check_backup_created(volume_name, job_name)

    def delete_expired_pending_recurringjob_pod(self, job_name, namespace=LONGHORN_NAMESPACE, expiration_seconds=5 * 60):
        """
        Deletes expired pending recurring job pods in the specified namespace.

        This method lists all pods in the given namespace with the label
        `recurring-job.longhorn.io={job_name}` and status `Pending`.
        It calculates the age of each pod and deletes those that have been
        pending for longer than the specified expiration time.

        This is a workaround for an upstream issue where CronJob pods may remain
        stuck in the `Pending` state indefinitely after a node reboot.
        Issue: https://github.com/longhorn/longhorn/issues/7956
        """
        try:
            pod_list = self.core_v1_api.list_namespaced_pod(
                namespace=namespace,
                label_selector=f"recurring-job.longhorn.io={job_name}"
            )
            for pod in pod_list.items:
                if pod.status.phase != "Pending":
                    continue

                pod_age = time.time() - pod.metadata.creation_timestamp.timestamp()
                if pod_age > expiration_seconds:
                    logging(f"Deleting expired pending recurringjob pod {pod.metadata.name}")
                    self.core_v1_api.delete_namespaced_pod(
                        name=pod.metadata.name,
                        namespace=namespace,
                        grace_period_seconds=0
                    )
                    logging(f"Deleted pod {pod.metadata.name}")
                else:
                    logging(f"Recurringjob pod {pod.metadata.name} in Pending state,\
                        age {pod_age:.2f} < expiration {expiration_seconds}s")
        except Exception as e:
            logging(f"Failed to delete expired pending recurringjob pods: {e}")

    def _check_snapshot_created(self, volume_name, job_name):
        # check snapshot can be created by the recurringjob
        current_time = datetime.utcnow()
        current_timestamp = current_time.timestamp()
        label_selector=f"longhornvolume={volume_name}"
        snapshot_timestamp = 0
        for i in range(self.retry_count):
            self.delete_expired_pending_recurringjob_pod(job_name)

            logging(f"Waiting for {volume_name} new snapshot created ({i}) ...")
            snapshot_list = filter_cr("longhorn.io", "v1beta2", "longhorn-system", "snapshots", label_selector=label_selector)
            try:
                if len(snapshot_list['items']) > 0:
                    for item in snapshot_list['items']:
                        # this snapshot can be created by snapshot or backup recurringjob
                        # but job_name is in spec.labels.RecurringJob
                        # and crd doesn't support field selector
                        # so need to filter by ourselves
                        if item['spec']['labels'] and 'RecurringJob' in item['spec']['labels'] and \
                            item['spec']['labels']['RecurringJob'] == job_name and \
                            item['status']['readyToUse'] == True:
                            snapshot_time = item['metadata']['creationTimestamp']
                            snapshot_time = datetime.strptime(snapshot_time, '%Y-%m-%dT%H:%M:%SZ')
                            snapshot_timestamp = snapshot_time.timestamp()
                        if snapshot_timestamp > current_timestamp:
                            logging(f"Got snapshot {item}, create time = {snapshot_time}, timestamp = {snapshot_timestamp}")
                            return
            except Exception as e:
                logging(f"Iterating snapshot list error: {e}")
            time.sleep(self.retry_interval)
        assert False, f"since {current_time},\
                        there's no new snapshot created by recurringjob \
                        {snapshot_list}"

    def _check_backup_created(self, volume_name, job_name, retry_count=-1):
        if retry_count == -1:
            retry_count = self.retry_count

        # check backup can be created by the recurringjob
        current_time = datetime.utcnow()
        current_timestamp = current_time.timestamp()
        label_selector=f"backup-volume={volume_name}"
        backup_timestamp = 0
        for i in range(retry_count):
            self.delete_expired_pending_recurringjob_pod(job_name)

            logging(f"Waiting for {volume_name} new backup created ({i}) ...")
            backup_list = filter_cr("longhorn.io", "v1beta2", "longhorn-system", "backups", label_selector=label_selector)
            try:
                if len(backup_list['items']) > 0:
                    for item in backup_list['items']:
                        state = item['status']['state']
                        if state != "Completed":
                            continue
                        backup_time = item['metadata']['creationTimestamp']
                        backup_time = datetime.strptime(backup_time, '%Y-%m-%dT%H:%M:%SZ')
                        backup_timestamp = backup_time.timestamp()
                        if backup_timestamp > current_timestamp:
                            logging(f"Got backup {item}, create time = {backup_time}, timestamp = {backup_timestamp}")
                            return
            except Exception as e:
                logging(f"Iterating backup list error: {e}")
            time.sleep(self.retry_interval)
        assert False, f"since {current_time},\
                        there's no new backup created by recurringjob \
                        {backup_list}"

    def cleanup(self, volume_names):
        for volume_name in volume_names:
            logging(f"Cleaning up recurringjobs for volume {volume_name}")
            jobs, _ = self.get_volume_recurringjobs_and_groups(volume_name)
            for job in jobs:
                logging(f"Deleting recurringjob {job}")
                self.delete(job, volume_name)

    def wait_for_systembackup_state(self, job_name, expected_state):
        return NotImplemented

    def assert_volume_backup_created(self, volume_name, job_name, retry_count=-1):
        self._check_backup_created(volume_name, job_name, retry_count=retry_count)

    def wait_for_pod_completion_without_error(self, job_name, namespace=LONGHORN_NAMESPACE):
        return NotImplemented
