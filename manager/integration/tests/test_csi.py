#!/usr/sbin/python
import pytest
import subprocess
import random
import common
from common import client, core_api, csi_pv, pod_make, pvc, storage_class  # NOQA
from common import pod as pod_manifest  # NOQA
from common import Gi, DEFAULT_VOLUME_SIZE, EXPANDED_VOLUME_SIZE
from common import VOLUME_RWTEST_SIZE
from common import create_and_wait_pod, create_pvc_spec, delete_and_wait_pod
from common import size_to_string, create_storage_class, create_pvc
from common import delete_and_wait_pvc, delete_and_wait_pv
from common import wait_and_get_pv_for_pvc
from common import generate_random_data, read_volume_data
from common import write_pod_volume_data
from common import write_pod_block_volume_data, read_pod_block_volume_data
from common import get_pod_data_md5sum
from common import generate_volume_name, create_and_check_volume
from common import delete_backup
from common import create_snapshot
from common import expand_and_wait_for_pvc, wait_for_volume_expansion
from common import get_volume_engine, wait_for_volume_detached
from common import create_pv_for_volume, create_pvc_for_volume
from common import get_self_host_id, get_volume_endpoint
from common import wait_for_volume_healthy
from common import fail_replica_expansion, wait_for_expansion_failure


# Using a StorageClass because GKE is using the default StorageClass if not
# specified. Volumes are still being manually created and not provisioned.
CSI_PV_TEST_STORAGE_NAME = 'longhorn-csi-pv-test'


def create_pv_storage(api, cli, pv, claim, base_image, from_backup):
    """
    Manually create a new PV and PVC for testing.
    """
    cli.create_volume(
        name=pv['metadata']['name'], size=pv['spec']['capacity']['storage'],
        numberOfReplicas=int(pv['spec']['csi']['volumeAttributes']
                             ['numberOfReplicas']),
        baseImage=base_image, fromBackup=from_backup)
    common.wait_for_volume_restoration_completed(cli, pv['metadata']['name'])
    common.wait_for_volume_detached(cli, pv['metadata']['name'])

    api.create_persistent_volume(pv)
    api.create_namespaced_persistent_volume_claim(
        body=claim,
        namespace='default')


def update_storageclass_references(name, pv, claim):
    """
    Rename all references to a StorageClass to a specified name.
    """
    pv['spec']['storageClassName'] = name
    claim['spec']['storageClassName'] = name


def create_and_wait_csi_pod(pod_name, client, core_api, csi_pv, pvc, pod_make, base_image, from_backup):  # NOQA
    pv_name = generate_volume_name()
    create_and_wait_csi_pod_named_pv(pv_name, pod_name, client, core_api,
                                     csi_pv, pvc, pod_make, base_image,
                                     from_backup)


def create_and_wait_csi_pod_named_pv(pv_name, pod_name, client, core_api, csi_pv, pvc, pod_make, base_image, from_backup):  # NOQA
    pod = pod_make(name=pod_name)
    pod['spec']['volumes'] = [
        create_pvc_spec(pv_name)
    ]
    csi_pv['metadata']['name'] = pv_name
    csi_pv['spec']['csi']['volumeHandle'] = pv_name
    csi_pv['spec']['csi']['volumeAttributes']['fromBackup'] = from_backup
    pvc['metadata']['name'] = pv_name
    pvc['spec']['volumeName'] = pv_name
    update_storageclass_references(CSI_PV_TEST_STORAGE_NAME, csi_pv, pvc)

    create_pv_storage(core_api, client, csi_pv, pvc, base_image, from_backup)
    create_and_wait_pod(core_api, pod)


@pytest.mark.coretest   # NOQA
@pytest.mark.csi  # NOQA
def test_csi_mount(client, core_api, csi_pv, pvc, pod_make):  # NOQA
    """
    Test that a statically defined CSI volume can be created, mounted,
    unmounted, and deleted properly on the Kubernetes cluster.

    Note: Fixtures are torn down here in reverse order that they are specified
    as a parameter. Take caution when reordering test fixtures.

    1. Create a PV/PVC/Pod with pre-created Longhorn volume
        1. Using Kubernetes manifest instead of Longhorn PV/PVC creation API
    2. Make sure the pod is running
    3. Verify the volume status
    """
    volume_size = DEFAULT_VOLUME_SIZE * Gi
    csi_mount_test(client, core_api,
                   csi_pv, pvc, pod_make, volume_size)


def csi_mount_test(client, core_api, csi_pv, pvc, pod_make,  # NOQA
                   volume_size, base_image=""):  # NOQA
    create_and_wait_csi_pod('csi-mount-test', client, core_api, csi_pv, pvc,
                            pod_make, base_image, "")

    volumes = client.list_volume().data
    assert len(volumes) == 1
    assert volumes[0].name == csi_pv['metadata']['name']
    assert volumes[0].size == str(volume_size)
    assert volumes[0].numberOfReplicas == \
        int(csi_pv['spec']['csi']['volumeAttributes']["numberOfReplicas"])
    assert volumes[0].state == "attached"
    assert volumes[0].baseImage == base_image


@pytest.mark.csi  # NOQA
def test_csi_io(client, core_api, csi_pv, pvc, pod_make):  # NOQA
    """
    Test that input and output on a statically defined CSI volume works as
    expected.

    Note: Fixtures are torn down here in reverse order that they are specified
    as a parameter. Take caution when reordering test fixtures.

    1. Create PV/PVC/Pod with dynamic positioned Longhorn volume
    2. Generate `test_data` and write it to volume using the equivalent
    of `kubectl exec`
    3. Delete the Pod
    4. Create another pod with the same PV
    5. Check the previous created `test_data` in the new Pod
    """
    csi_io_test(client, core_api, csi_pv, pvc, pod_make)


def csi_io_test(client, core_api, csi_pv, pvc, pod_make, base_image=""):  # NOQA
    pv_name = generate_volume_name()
    pod_name = 'csi-io-test'
    create_and_wait_csi_pod_named_pv(pv_name, pod_name, client, core_api,
                                     csi_pv, pvc, pod_make, base_image, "")

    test_data = generate_random_data(VOLUME_RWTEST_SIZE)
    write_pod_volume_data(core_api, pod_name, test_data)
    delete_and_wait_pod(core_api, pod_name)
    common.wait_for_volume_detached(client, csi_pv['metadata']['name'])

    pod_name = 'csi-io-test-2'
    pod = pod_make(name=pod_name)
    pod['spec']['volumes'] = [
        create_pvc_spec(pv_name)
    ]
    csi_pv['metadata']['name'] = pv_name
    csi_pv['spec']['csi']['volumeHandle'] = pv_name
    pvc['metadata']['name'] = pv_name
    pvc['spec']['volumeName'] = pv_name
    update_storageclass_references(CSI_PV_TEST_STORAGE_NAME, csi_pv, pvc)

    create_and_wait_pod(core_api, pod)

    resp = read_volume_data(core_api, pod_name)
    assert resp == test_data


@pytest.mark.csi  # NOQA
def test_csi_backup(client, core_api, csi_pv, pvc, pod_make):  # NOQA
    """
    Test that backup/restore works with volumes created by CSI driver.

    Run the test for all the backupstores

    1. Create PV/PVC/Pod using dynamic provisioned volume
    2. Write data and create snapshot using Longhorn API
    3. Verify the existence of backup
    4. Create another Pod using restored backup
    5. Verify the data in the new Pod
    """
    csi_backup_test(client, core_api, csi_pv, pvc, pod_make)


def csi_backup_test(client, core_api, csi_pv, pvc, pod_make, base_image=""):  # NOQA
    pod_name = 'csi-backup-test'
    create_and_wait_csi_pod(pod_name, client, core_api, csi_pv, pvc, pod_make,
                            base_image, "")
    test_data = generate_random_data(VOLUME_RWTEST_SIZE)

    setting = client.by_id_setting(common.SETTING_BACKUP_TARGET)
    # test backupTarget for multiple settings
    backupstores = common.get_backupstore_url()
    i = 1
    for backupstore in backupstores:
        if common.is_backupTarget_s3(backupstore):
            backupsettings = backupstore.split("$")
            setting = client.update(setting, value=backupsettings[0])
            assert setting.value == backupsettings[0]

            credential = client.by_id_setting(
                common.SETTING_BACKUP_TARGET_CREDENTIAL_SECRET)
            credential = client.update(credential, value=backupsettings[1])
            assert credential.value == backupsettings[1]
        else:
            setting = client.update(setting, value=backupstore)
            assert setting.value == backupstore
            credential = client.by_id_setting(
                common.SETTING_BACKUP_TARGET_CREDENTIAL_SECRET)
            credential = client.update(credential, value="")
            assert credential.value == ""

        backupstore_test(client, core_api, csi_pv, pvc, pod_make, pod_name,
                         base_image, test_data, i)
        i += 1


def backupstore_test(client, core_api, csi_pv, pvc, pod_make, pod_name, base_image, test_data, i):  # NOQA
    vol_name = csi_pv['metadata']['name']
    write_pod_volume_data(core_api, pod_name, test_data)

    volume = client.by_id_volume(vol_name)
    snap = create_snapshot(client, vol_name)
    volume.snapshotBackup(name=snap.name)

    common.wait_for_backup_completion(client, vol_name, snap.name)
    bv, b = common.find_backup(client, vol_name, snap.name)

    pod2_name = 'csi-backup-test-' + str(i)
    create_and_wait_csi_pod(pod2_name, client, core_api, csi_pv, pvc, pod_make,
                            base_image, b.url)

    resp = read_volume_data(core_api, pod2_name)
    assert resp == test_data

    delete_backup(client, bv.name, b.name)


@pytest.mark.csi  # NOQA
def test_csi_block_volume(client, core_api, storage_class, pvc, pod_manifest):  # NOQA
    """
    Test CSI feature: raw block volume

    1. Create a PVC with `volumeMode = Block`
    2. Create a pod using the PVC to dynamic provision a volume
    3. Verify the pod creation
    4. Generate `test_data` and write to the block volume directly in the pod
    5. Read the data back for validation
    6. Delete the pod and create `pod2` to use the same volume
    7. Validate the data in `pod2` is consistent with `test_data`
    """
    pod_name = 'csi-block-volume-test'
    pvc_name = pod_name + "-pvc"
    device_path = "/dev/longhorn/longhorn-test-blk"

    storage_class['reclaimPolicy'] = 'Retain'
    pvc['metadata']['name'] = pvc_name
    pvc['spec']['volumeMode'] = 'Block'
    pvc['spec']['storageClassName'] = storage_class['metadata']['name']
    pvc['spec']['resources'] = {
        'requests': {
            'storage': size_to_string(1 * Gi)
        }
    }
    pod_manifest['metadata']['name'] = pod_name
    pod_manifest['spec']['volumes'] = [{
        'name': 'longhorn-blk',
        'persistentVolumeClaim': {
            'claimName': pvc_name,
        },
    }]
    pod_manifest['spec']['containers'][0]['volumeMounts'] = []
    pod_manifest['spec']['containers'][0]['volumeDevices'] = [
        {'name': 'longhorn-blk', 'devicePath': device_path}
    ]

    create_storage_class(storage_class)
    create_pvc(pvc)
    pv_name = wait_and_get_pv_for_pvc(core_api, pvc_name).metadata.name
    create_and_wait_pod(core_api, pod_manifest)

    test_data = generate_random_data(VOLUME_RWTEST_SIZE)
    test_offset = random.randint(0, VOLUME_RWTEST_SIZE)
    write_pod_block_volume_data(
        core_api, pod_name, test_data, test_offset, device_path)
    returned_data = read_pod_block_volume_data(
        core_api, pod_name, len(test_data), test_offset, device_path
    )
    assert test_data == returned_data
    md5_sum = get_pod_data_md5sum(
        core_api, pod_name, device_path)

    delete_and_wait_pod(core_api, pod_name)
    common.wait_for_volume_detached(client, pv_name)

    pod_name_2 = 'csi-block-volume-test-reuse'
    pod_manifest['metadata']['name'] = pod_name_2
    create_and_wait_pod(core_api, pod_manifest)

    returned_data = read_pod_block_volume_data(
        core_api, pod_name_2, len(test_data), test_offset, device_path
    )
    assert test_data == returned_data
    md5_sum_2 = get_pod_data_md5sum(
        core_api, pod_name_2, device_path)
    assert md5_sum == md5_sum_2

    delete_and_wait_pod(core_api, pod_name_2)
    delete_and_wait_pvc(core_api, pvc_name)
    delete_and_wait_pv(core_api, pv_name)


@pytest.mark.coretest   # NOQA
@pytest.mark.csi  # NOQA
@pytest.mark.csi_expansion  # NOQA
def test_csi_offline_expansion(client, core_api, storage_class, pvc, pod_manifest):  # NOQA
    """
    Test CSI feature: offline expansion

    1. Create a new `storage_class` with `allowVolumeExpansion` set
    2. Create PVC and Pod with dynamic provisioned volume from the StorageClass
    3. Generate `test_data` and write to the pod
    4. Delete the pod
    5. Update pvc.spec.resources to expand the volume
    6. Verify the volume expansion done using Longhorn API
    7. Create a new pod and validate the volume content
    """
    create_storage_class(storage_class)

    pod_name = 'csi-offline-expand-volume-test'
    pvc_name = pod_name + "-pvc"
    pvc['metadata']['name'] = pvc_name
    pvc['spec']['storageClassName'] = storage_class['metadata']['name']
    create_pvc(pvc)

    pod_manifest['metadata']['name'] = pod_name
    pod_manifest['spec']['volumes'] = [{
        'name':
            pod_manifest['spec']['containers'][0]['volumeMounts'][0]['name'],
        'persistentVolumeClaim': {'claimName': pvc_name},
    }]
    create_and_wait_pod(core_api, pod_manifest)
    test_data = generate_random_data(VOLUME_RWTEST_SIZE)
    write_pod_volume_data(core_api, pod_name, test_data)
    delete_and_wait_pod(core_api, pod_name)

    pv = wait_and_get_pv_for_pvc(core_api, pvc_name)
    assert pv.status.phase == "Bound"
    volume_name = pv.spec.csi.volume_handle
    wait_for_volume_detached(client, volume_name)

    pvc['spec']['resources'] = {
        'requests': {
            'storage': size_to_string(EXPANDED_VOLUME_SIZE*Gi)
        }
    }
    expand_and_wait_for_pvc(core_api, pvc)
    wait_for_volume_expansion(client, volume_name)
    volume = client.by_id_volume(volume_name)
    assert volume.state == "detached"
    assert volume.size == str(EXPANDED_VOLUME_SIZE*Gi)

    pod_manifest['metadata']['name'] = pod_name
    pod_manifest['spec']['volumes'] = [{
        'name':
            pod_manifest['spec']['containers'][0]['volumeMounts'][0]['name'],
        'persistentVolumeClaim': {'claimName': pvc_name},
    }]
    create_and_wait_pod(core_api, pod_manifest)

    resp = read_volume_data(core_api, pod_name)
    assert resp == test_data

    volume = client.by_id_volume(volume_name)
    engine = get_volume_engine(volume)
    assert volume.size == str(EXPANDED_VOLUME_SIZE*Gi)
    assert volume.size == engine.size


def test_xfs_pv(client, core_api, pod_manifest):  # NOQA
    """
    Test create PV with new XFS filesystem

    1. Create a volume
    2. Create a PV for the existing volume, specify `xfs` as filesystem
    3. Create PVC and Pod
    4. Make sure Pod is running.
    5. Write data into the pod and read back for validation.

    Note: The volume will be formatted to XFS filesystem by Kubernetes in this
    case.
    """
    volume_name = generate_volume_name()

    volume = create_and_check_volume(client, volume_name)

    create_pv_for_volume(client, core_api, volume, volume_name, "xfs")

    create_pvc_for_volume(client, core_api, volume, volume_name)

    pod_manifest['spec']['volumes'] = [{
        "name": "pod-data",
        "persistentVolumeClaim": {
            "claimName": volume_name
        }
    }]

    pod_name = pod_manifest['metadata']['name']

    create_and_wait_pod(core_api, pod_manifest)

    test_data = generate_random_data(VOLUME_RWTEST_SIZE)
    write_pod_volume_data(core_api, pod_name, test_data)
    resp = read_volume_data(core_api, pod_name)
    assert resp == test_data


def test_xfs_pv_existing_volume(client, core_api, pod_manifest):  # NOQA
    """
    Test create PV with existing XFS filesystem

    1. Create a volume
    2. Create PV/PVC for the existing volume, specify `xfs` as filesystem
    3. Attach the volume to the current node.
    4. Format it to `xfs`
    5. Create a POD using the volume

    FIXME: We should write data in step 4 and validate the data in step 5, make
    sure the disk won't be reformatted
    """
    volume_name = generate_volume_name()

    volume = create_and_check_volume(client, volume_name)

    create_pv_for_volume(client, core_api, volume, volume_name, "xfs")

    create_pvc_for_volume(client, core_api, volume, volume_name)

    host_id = get_self_host_id()

    volume = volume.attach(hostId=host_id)

    volume = wait_for_volume_healthy(client, volume_name)

    cmd = ['mkfs.xfs', get_volume_endpoint(volume)]
    subprocess.check_call(cmd)

    volume = volume.detach()

    volume = wait_for_volume_detached(client, volume_name)

    pod_manifest['spec']['volumes'] = [{
        "name": "pod-data",
        "persistentVolumeClaim": {
            "claimName": volume_name
        }
    }]

    create_and_wait_pod(core_api, pod_manifest)


@pytest.mark.coretest  # NOQA
def test_csi_expansion_with_replica_failure(client, core_api, storage_class, pvc, pod_manifest):  # NOQA
    """
    Test expansion success but with one replica expansion failure

    1. Create a new `storage_class` with `allowVolumeExpansion` set
    2. Create PVC and Pod with dynamic provisioned volume from the StorageClass
    3. Create an empty directory with expansion snapshot tmp meta file path
       for one replica so that the replica expansion will fail
    4. Generate `test_data` and write to the pod
    5. Delete the pod and wait for volume detachment
    6. Update pvc.spec.resources to expand the volume
    7. Check expansion result using Longhorn API. There will be expansion error
       caused by the failed replica but overall the expansion should succeed.
    8. Create a new pod and
       check if the volume will rebuild the failed replica
    9. Validate the volume content, then check if data writing looks fine
    """
    create_storage_class(storage_class)

    pod_name = 'csi-expansion-with-replica-failure-test'
    pvc_name = pod_name + "-pvc"
    pvc['metadata']['name'] = pvc_name
    pvc['spec']['storageClassName'] = storage_class['metadata']['name']
    create_pvc(pvc)

    pod_manifest['metadata']['name'] = pod_name
    pod_manifest['spec']['volumes'] = [{
        'name':
            pod_manifest['spec']['containers'][0]['volumeMounts'][0]['name'],
        'persistentVolumeClaim': {'claimName': pvc_name},
    }]
    create_and_wait_pod(core_api, pod_manifest)

    expand_size = str(EXPANDED_VOLUME_SIZE*Gi)
    pv = wait_and_get_pv_for_pvc(core_api, pvc_name)
    assert pv.status.phase == "Bound"
    volume_name = pv.spec.csi.volume_handle
    volume = client.by_id_volume(volume_name)
    failed_replica = volume.replicas[0]
    fail_replica_expansion(client, core_api,
                           volume_name, expand_size, [failed_replica])

    test_data = generate_random_data(VOLUME_RWTEST_SIZE)
    write_pod_volume_data(core_api, pod_name, test_data)

    delete_and_wait_pod(core_api, pod_name)
    wait_for_volume_detached(client, volume_name)

    # There will be replica expansion error info
    # but the expansion should succeed.
    pvc['spec']['resources'] = {
        'requests': {
            'storage': size_to_string(EXPANDED_VOLUME_SIZE*Gi)
        }
    }
    expand_and_wait_for_pvc(core_api, pvc)
    wait_for_expansion_failure(client, volume_name)
    wait_for_volume_expansion(client, volume_name)
    volume = client.by_id_volume(volume_name)
    assert volume.state == "detached"
    assert volume.size == expand_size
    for r in volume.replicas:
        if r.name == failed_replica.name:
            assert r.failedAt != ""
        else:
            assert r.failedAt == ""

    # Check if the replica will be rebuilded
    # and if the volume still works fine.
    create_and_wait_pod(core_api, pod_manifest)
    volume = wait_for_volume_healthy(client, volume_name)
    for r in volume.replicas:
        if r.name == failed_replica.name:
            assert r.mode == ""
        else:
            assert r.mode == "RW"
    resp = read_volume_data(core_api, pod_name)
    assert resp == test_data
    test_data = generate_random_data(VOLUME_RWTEST_SIZE)
    write_pod_volume_data(core_api, pod_name, test_data)
    resp = read_volume_data(core_api, pod_name)
    assert resp == test_data


@pytest.mark.skip(reason="TODO")  # NOQA
def test_csi_volumesnapshot_basic():  # NOQA
    """
    Test creation / restoration / deletion of a backup via the csi snapshotter

    Context:

    We want to allow the user to programmatically create/restore/delete
    longhorn backups via the csi snapshot mechanism
    ref: https://kubernetes.io/docs/concepts/storage/volume-snapshots/

    Setup:

    1. Make sure your cluster contains the below crds
    https://github.com/kubernetes-csi/external-snapshotter
    /tree/master/client/config/crd
    2. Make sure your cluster contains the snapshot controller
    https://github.com/kubernetes-csi/external-snapshotter
    /tree/master/deploy/kubernetes/snapshot-controller

    Steps:

    def csi_volumesnapshot_creation_test(snapshotClass=longhorn|custom):
    1. create volume(1)
    2. write data to volume(1)
    3. create a kubernetes `VolumeSnapshot` object
       the `VolumeSnapshot.uuid` will be used to identify a
       **longhorn snapshot** and the associated `VolumeSnapshotContent` object
    4. check creation of a new longhorn snapshot named `snapshot-uuid`
    5. check for `VolumeSnapshotContent` named `snapcontent-uuid`
    6. wait for `VolumeSnapshotContent.readyToUse` flag to be set to **true**
    7. check for backup existance on the backupstore

    # the csi snapshot restore sets the fromBackup field same as
    # the StorageClass based restore approach.
    def csi_volumesnapshot_restore_test():
    8. create a `PersistentVolumeClaim` object where the `dataSource` field
       references the `VolumeSnapshot` object by name
    9. verify creation of a new volume(2) bound to the pvc created in step(8)
    10. verify data of new volume(2) equals data
        from backup (ie old data above)

    # default longhorn snapshot class is set to Delete
    # add a second test with a custom snapshot class with deletionPolicy
    # set to Retain you can reuse these methods for that and other tests
    def csi_volumesnapshot_deletion_test(deletionPolicy='Delete|Retain'):
    11. delete `VolumeSnapshot` object
    12. if deletionPolicy == Delete:
        13. verify deletion of `VolumeSnapshot` and
            `VolumeSnapshotContent` objects
        14. verify deletion of backup from backupstore
    12. if deletionPolicy == Retain:
        13. verify deletion of `VolumeSnapshot`
        14. verify retention of `VolumeSnapshotContent`
            and backup on backupstore

    15. cleanup
    """


@pytest.mark.skip(reason="TODO")  # NOQA
def test_csi_volumesnapshot_restore_existing_backup():  # NOQA
    """
    Test restoration of a non csi created backup via the csi snapshotter

    Context:

    We want to allow the user to programmatically create/restore/delete
    longhorn backups via the csi snapshot mechanism
    ref: https://kubernetes.io/docs/concepts/storage/volume-snapshots/

    Setup:

    1. Make sure your cluster contains the below crds
    https://github.com/kubernetes-csi/external-snapshotter
    /tree/master/client/config/crd
    2. Make sure your cluster contains the snapshot controller
    https://github.com/kubernetes-csi/external-snapshotter
    /tree/master/deploy/kubernetes/snapshot-controller

    Steps:
    1. create volume(1)
    2. write data to volume(1)
    3. create backup of volume(1)
    4. wait for backup completion

    # see example yamls, in the longhorn-manager repo
    5. create `VolumeSnapshotContent` and `snapshotHandle` set as
       `bs://backup-volume/backup-name`
    6. create the associated `VolumeSnapshot` object where the `source` field
       refers to a `VolumeSnapshotContent` object via the
       `volumeSnapshotContentName` field.
       This differs from the creation of a backup in which case the
       `source` field refers to a `PersistentVolumeClaim` via the
       `persistentVolumeClaimName` field. Only one type of reference
       can be set for a `VolumeSnapshot` object.

    7. call csi_volumesnapshot_restore_test()
    8. call csi_volumesnapshot_deletion_test()

    9. cleanup
    """


@pytest.mark.skip(reason="TODO")  # NOQA
def test_csi_volumesnapshot_deletion_retain():  # NOQA
    """
    Test retention of a backup while deleting the associated `VolumeSnapshot`
    via the csi snapshotter

    Context:

    We want to allow the user to programmatically create/restore/delete
    longhorn backups via the csi snapshot mechanism
    ref: https://kubernetes.io/docs/concepts/storage/volume-snapshots/

    Setup:

    1. Make sure your cluster contains the below crds
    https://github.com/kubernetes-csi/external-snapshotter
    /tree/master/client/config/crd
    2. Make sure your cluster contains the snapshot controller
    https://github.com/kubernetes-csi/external-snapshotter
    /tree/master/deploy/kubernetes/snapshot-controller

    Steps:

    1. create new snapshotClass with deletionPolicy set to Retain
    2. call csi_volumesnapshot_creation_test(snapshotClass=custom)
    3. call csi_volumesnapshot_restore_test()
    4. call csi_volumesnapshot_deletion_test(deletionPolicy='Retain'):
    5. cleanup
    """


@pytest.mark.skip(reason="TODO")  # NOQA
@pytest.mark.coretest
def test_allow_volume_creation_with_degraded_availability_csi():
    """
    Test Allow Volume Creation with Degraded Availability (CSI)

    Requirement:
    1. Set `allow-volume-creation-with-degraded-availability` to true
    2. `node-level-soft-anti-affinity` to false

    Steps:
    1. Disable scheduling for node 3
    2. Create a Deployment Pod with a volume and three replicas.
        1. After the volume is attached, scheduling error should be seen.
    3. Write data to the Pod.
    4. Scale down the deployment to 0 to detach the volume.
        1. Scheduled condition should become true.
    5. Scale up the deployment back to 1 verify the data.
        1. Scheduled condition should become false.
    6. Enable the scheduling for the third node.
        1. Volume should start rebuilding on the third node soon.
        2. Once the rebuilding starts, the scheduled condition should become
        true
    7. Once rebuild finished, scale down and back the deployment to
        verify the data.
    """


@pytest.mark.skip(reason="TODO")  # NOQA
def test_csi_minimal_volume_size(): # NOQA
    """
    Test CSI Minimal Volume Size

    1. Create a PVC with size 5MiB. Check the PVC should get size 10MiB.
    Remove the PVC.
    2. Create a PVC with size 10MiB. Check the PVC should get size 10MiB.
    3. Create a pod to use this PVC, and generate some data with checksum.
    4. Write the data to the volume and read it back to compare the checksum.
    5. Delete the pod and the PVC.
    """
