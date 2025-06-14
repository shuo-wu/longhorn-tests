*** Settings ***
Documentation    Negative Test Cases

Test Tags    negative

Resource    ../keywords/variables.resource
Resource    ../keywords/common.resource
Resource    ../keywords/persistentvolumeclaim.resource
Resource    ../keywords/statefulset.resource
Resource    ../keywords/stress.resource
Resource    ../keywords/volume.resource
Resource    ../keywords/workload.resource

Test Setup    Set up test environment
Test Teardown    Cleanup test resources

*** Test Cases ***
Stress Volume Node Filesystem When Replica Is Rebuilding
    Given Create volume 0 with    size=5Gi    numberOfReplicas=3
    And Attach volume 0
    And Write data to volume 0
    And Stress filesystem of volume 0 volume node

    FOR    ${i}    IN RANGE    ${LOOP_COUNT}
        When Delete volume 0 replica on volume node
        And Wait until volume 0 replica rebuilding started on volume node

        Then Wait until volume 0 replica rebuilding completed on volume node
        And Check volume 0 data is intact
    END

Stress Volume Node Filesystem When Volume Is Detaching and Attaching
    Given Create volume 0 with    size=5Gi    numberOfReplicas=3
    And Attach volume 0
    And Write data to volume 0
    And Stress filesystem of volume 0 volume node

    FOR    ${i}    IN RANGE    ${LOOP_COUNT}
        And Detach volume 0
        And Wait for volume 0 detached
        And Attach volume 0
        And Wait for volume 0 healthy
        And Check volume 0 data is intact
    END

Stress Volume Node Filesystem When Volume Is Online Expanding
    Given Create statefulset 0 using RWO volume
    And Write 1024 MB data to file data.txt in statefulset 0
    And Stress filesystem of statefulset 0 volume node

    FOR    ${i}    IN RANGE    ${LOOP_COUNT}
        When Expand statefulset 0 volume with additional 100 MiB
        Then Wait for statefulset 0 volume size expanded

        And Check statefulset 0 data in file data.txt is intact
    END

Stress Volume Node Filesystem When Volume Is Offline Expanding
    Given Create statefulset 0 using RWO volume
    And Write 1024 MB data to file data.txt in statefulset 0
    And Stress filesystem of all worker nodes

    FOR    ${i}    IN RANGE    ${LOOP_COUNT}
        And Scale down statefulset 0 to detach volume

        When Expand statefulset 0 volume with additional 100 MiB
        Then Wait for statefulset 0 volume size expanded
        And Wait for statefulset 0 volume detached
        And Scale up statefulset 0 to attach volume
        And Wait for volume of statefulset 0 healthy
        And Check statefulset 0 data in file data.txt is intact
    END
