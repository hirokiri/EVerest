#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright Pionix GmbH and Contributors to EVerest

import logging
import threading
import time
from collections import defaultdict

import pytest

from everest.testing.core_utils.everest_core import EverestCore
from everest.testing.core_utils.fixtures import *


def topic_count_matching(topic_counts, topic_predicate):
    return sum(count for topic, count in topic_counts.items() if topic_predicate(topic))


def wait_for_matching_topic_count(topic_counts, topic_predicate, expected_count: int, timeout_s: float = 60.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        current_count = topic_count_matching(topic_counts, topic_predicate)
        if current_count >= expected_count:
            return
        time.sleep(0.2)
    raise TimeoutError(
        f"Timeout waiting for matching topics to reach count {expected_count}. "
        f"Current count is {topic_count_matching(topic_counts, topic_predicate)}."
    )


def wait_for_log_message(caplog, needle: str, timeout_s: float = 20.0, start_index: int = 0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if any(needle in record.getMessage() for record in caplog.records[start_index:]):
            return
        time.sleep(0.05)
    raise TimeoutError(f"Timeout waiting for log message containing '{needle}'.")


def assert_no_log_message(caplog, needle: str, timeout_s: float = 5.0, start_index: int = 0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if any(needle in record.getMessage() for record in caplog.records[start_index:]):
            raise AssertionError(f"Unexpected log message containing '{needle}'.")
        time.sleep(0.05)


@pytest.mark.everest_core_config("config-sil-immortal_manager.yaml")
def test_manager_stop_during_startup(everest_core: EverestCore, caplog):
    start_exception = []

    def start_core():
        try:
            everest_core.start()
        except Exception as exc:
            start_exception.append(exc)

    with caplog.at_level(logging.DEBUG):
        starter_thread = threading.Thread(target=start_core)
        starter_thread.start()

        wait_for_log_message(caplog, "Manager state transition: Initializing -> StartingModules", timeout_s=60.0)
        log_marker = len(caplog.records)
        everest_core.stop()
        starter_thread.join(timeout=60.0)

    assert not starter_thread.is_alive(), "Startup thread did not exit after stop()."
    with caplog.at_level(logging.DEBUG):
        wait_for_log_message(caplog, "SIGINT/SIGTERM received", timeout_s=60.0, start_index=log_marker)


@pytest.mark.everest_core_config("config-sil-immortal_manager.yaml")
def test_manager_graceful_shutdown_from_running(everest_core: EverestCore, caplog):
    with caplog.at_level(logging.DEBUG):
        everest_core.start()
        wait_for_log_message(caplog, "Manager state transition: StartingModules -> Running", timeout_s=60.0)

    log_marker = len(caplog.records)
    everest_core.stop()

    with caplog.at_level(logging.DEBUG):
        wait_for_log_message(caplog, "SIGINT/SIGTERM received", timeout_s=60.0, start_index=log_marker)
        wait_for_log_message(caplog, "All modules shut down properly", timeout_s=60.0, start_index=log_marker)


@pytest.mark.everest_core_config("config-sil-immortal_manager.yaml")
@pytest.mark.everest_manager_args("--into-idle")
def test_manager_into_idle_skips_module_startup(everest_core: EverestCore, caplog):
    start_exception = []

    def start_core():
        try:
            everest_core.start()
        except Exception as exc:
            start_exception.append(exc)

    with caplog.at_level(logging.DEBUG):
        starter_thread = threading.Thread(target=start_core)
        starter_thread.start()
        wait_for_log_message(caplog, "Manager state transition: Initializing -> Idle", timeout_s=60.0)
        assert_no_log_message(
            caplog,
            "Manager state transition: StartingModules -> Running",
            timeout_s=5.0,
        )
        everest_core.stop()
        starter_thread.join(timeout=60.0)

    assert not starter_thread.is_alive(), "Startup thread did not exit after stop()."
    assert start_exception, "Expected start() to fail because no modules are started with --into-idle."
    assert not any(
        "Manager state transition: StartingModules -> Running" in record.getMessage()
        for record in caplog.records
    ), "Manager must not start modules when --into-idle is set."


@pytest.mark.everest_core_config("config-sil-immortal_manager.yaml")
@pytest.mark.everest_manager_args("--recover-module-crashes")
def test_manager_restarts_modules_after_unexpected_exit(
    everest_core: EverestCore, connected_mqtt_client, caplog
):
    topic_counts = defaultdict(int)
    mqtt_everest_prefix = f"everest_{everest_core.everest_uuid}"
    mqtt_external_prefix = everest_core.mqtt_external_prefix
    ready_topic_suffix = "/modules/exit_simulator/ready"
    ready_topic_filter = f"{mqtt_everest_prefix}/modules/+/ready"
    exit_cmd_topic = f"{mqtt_external_prefix}everest_api/exit_simulator/cmd/exit"
    ready_topic_predicate = lambda topic: topic.startswith(f"{mqtt_everest_prefix}/") and topic.endswith(ready_topic_suffix)

    def on_message(_client, _userdata, msg):
        topic_counts[msg.topic] += 1

    connected_mqtt_client.on_message = on_message
    connected_mqtt_client.subscribe(ready_topic_filter)
    time.sleep(0.2)

    everest_core.start()

    # wait for the exit simulator module to be ready
    wait_for_matching_topic_count(topic_counts, ready_topic_predicate, 1, timeout_s=60.0)
    baseline_ready = topic_count_matching(topic_counts, ready_topic_predicate)

    with caplog.at_level(logging.DEBUG):
        wait_for_log_message(caplog, "Manager state transition: StartingModules -> Running", timeout_s=60.0)

    log_marker = len(caplog.records)
    connected_mqtt_client.publish(exit_cmd_topic, "1")

    with caplog.at_level(logging.DEBUG):
        wait_for_log_message(
            caplog,
            "Manager state transition: StartingModules -> Running",
            timeout_s=60.0,
            start_index=log_marker,
        )

    wait_for_matching_topic_count(
        topic_counts,
        ready_topic_predicate,
        baseline_ready + 1,
        timeout_s=60.0,
    )


@pytest.mark.everest_core_config("config-sil-immortal_manager.yaml")
@pytest.mark.everest_manager_args("--recover-module-crashes")
def test_manager_restarts_modules_after_unexpected_exit_max_3_times(
    everest_core: EverestCore, connected_mqtt_client, caplog
):
    mqtt_external_prefix = everest_core.mqtt_external_prefix
    exit_cmd_topic = f"{mqtt_external_prefix}everest_api/exit_simulator/cmd/exit"

    with caplog.at_level(logging.DEBUG):
        everest_core.start()
        wait_for_log_message(caplog, "Manager state transition: StartingModules -> Running", timeout_s=60.0)

    expected_log_line_sets = [
        [
            "Manager state transition: ShutdownRequested -> CrashShutdownInProgress",
            "Unexpected module exit recovery attempt 1/3. Reloading config and restarting modules.",
            "Manager state transition: StartingModules -> Running",
        ],
        [
            "Unexpected module exit recovery attempt 2/3. Reloading config and restarting modules.",
            "Manager state transition: StartingModules -> Running",
        ],
        [
            "Unexpected module exit recovery attempt 3/3. Reloading config and restarting modules.",
            "Manager state transition: StartingModules -> Running",
        ],
        [
            "Reached maximum unexpected module exit recovery attempts (4/3). Manager will stay idle after shutdown.",
        ],
    ]

    log_marker = len(caplog.records)
    for loglines in expected_log_line_sets:
        connected_mqtt_client.publish(exit_cmd_topic, "1")
        for logline in loglines:
            with caplog.at_level(logging.DEBUG):
                wait_for_log_message(
                    caplog,
                    logline,
                    timeout_s=60.0,
                    start_index=log_marker,
                )
        log_marker = len(caplog.records)

    with caplog.at_level(logging.DEBUG):
        assert_no_log_message(
            caplog,
            "Unexpected module exit recovery attempt",
            timeout_s=10.0,
            start_index=log_marker,
        )
        assert_no_log_message(
            caplog,
            "Manager state transition: StartingModules -> Running",
            timeout_s=10.0,
            start_index=log_marker,
        )


@pytest.mark.everest_core_config("config-sil-immortal_manager.yaml")
def test_manager_does_not_transition_back_to_running_when_stopped_during_startup(
    everest_core: EverestCore, caplog
):
    start_exception = []

    def start_core():
        try:
            everest_core.start()
        except Exception as exc:
            start_exception.append(exc)

    with caplog.at_level(logging.DEBUG):
        starter_thread = threading.Thread(target=start_core)
        starter_thread.start()

        wait_for_log_message(caplog, "Manager state transition: Initializing -> StartingModules", timeout_s=60.0)
        everest_core.stop()
        starter_thread.join(timeout=60.0)

    assert not starter_thread.is_alive(), "Startup thread did not exit after stop()."
    assert all(isinstance(exc, TimeoutError) for exc in start_exception), (
        f"Unexpected startup exception(s): {start_exception}"
    )
    assert any(
        "Manager state transition: StartingModules -> ShutdownRequested" in record.getMessage()
        for record in caplog.records
    ), "Expected startup shutdown transition not observed."
    assert not any(
        "Manager state transition: ShutdownRequested -> Running" in record.getMessage()
        for record in caplog.records
    ), "Manager must not transition back to Running after shutdown was requested during startup."


@pytest.mark.everest_core_config("config-sil-immortal_manager.yaml")
@pytest.mark.everest_manager_args("--recover-module-crashes")
def test_manager_recovers_after_crash_with_blocked_module_timeout(
    everest_core: EverestCore, connected_mqtt_client, caplog
):
    topic_counts = defaultdict(int)
    mqtt_everest_prefix = f"everest_{everest_core.everest_uuid}"
    mqtt_external_prefix = everest_core.mqtt_external_prefix
    block_ready_topic_suffix = "/modules/block_simulator/ready"
    block_ready_topic_filter = f"{mqtt_everest_prefix}/modules/+/ready"
    block_cmd_topic = f"{mqtt_external_prefix}everest_api/block_simulator/cmd/block"
    exit_cmd_topic = f"{mqtt_external_prefix}everest_api/exit_simulator/cmd/exit"
    block_ready_topic_predicate = lambda topic: topic.startswith(f"{mqtt_everest_prefix}/") and topic.endswith(
        block_ready_topic_suffix
    )

    def on_message(_client, _userdata, msg):
        topic_counts[msg.topic] += 1

    connected_mqtt_client.on_message = on_message
    connected_mqtt_client.subscribe(block_ready_topic_filter)
    time.sleep(0.2)

    with caplog.at_level(logging.DEBUG):
        everest_core.start()
        wait_for_log_message(caplog, "Manager state transition: StartingModules -> Running", timeout_s=60.0)

    wait_for_matching_topic_count(topic_counts, block_ready_topic_predicate, 1, timeout_s=60.0)
    baseline_ready = topic_count_matching(topic_counts, block_ready_topic_predicate)
    connected_mqtt_client.publish(block_cmd_topic, "")
    time.sleep(0.5)

    log_marker = len(caplog.records)
    connected_mqtt_client.publish(exit_cmd_topic, "1")

    with caplog.at_level(logging.DEBUG):
        wait_for_log_message(
            caplog,
            "Not all modules shut down within the timeout. Forcefully terminating remaining modules.",
            timeout_s=60.0,
            start_index=log_marker,
        )
        wait_for_log_message(
            caplog,
            "Manager state transition: StartingModules -> Running",
            timeout_s=60.0,
            start_index=log_marker,
        )

    wait_for_matching_topic_count(
        topic_counts,
        block_ready_topic_predicate,
        baseline_ready + 1,
        timeout_s=60.0,
    )
