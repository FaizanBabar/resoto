import asyncio
import logging
import platform
import sys
import traceback
import warnings
from argparse import Namespace
from asyncio import Queue
from contextlib import suppress
from datetime import timedelta
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import AsyncIterator, List, Union

from aiohttp.web_app import Application
from arango.database import StandardDatabase
from attrs import evolve
from urllib3.exceptions import HTTPWarning

from resotocore import version
from resotocore.action_handlers.merge_outer_edge_handler import MergeOuterEdgesHandler
from resotocore.analytics import CoreEvent, NoEventSender
from resotocore.analytics.posthog import PostHogEventSender
from resotocore.analytics.recurrent_events import emit_recurrent_events
from resotocore.cli.cli import CLIService
from resotocore.cli.command import alias_names, all_commands
from resotocore.cli.dependencies import CLIDependencies
from resotocore.config.config_handler_service import ConfigHandlerService
from resotocore.config.config_override_service import ConfigOverrideService, model_from_db, override_config_for_startup
from resotocore.config.core_config_handler import CoreConfigHandler
from resotocore.core_config import (
    config_from_db,
    CoreConfig,
    RunConfig,
    inside_docker,
    inside_kubernetes,
    helm_installation,
    ResotoCoreConfigId,
)
from resotocore.db import SystemData
from resotocore.db.db_access import DbAccess
from resotocore.dependencies import db_access, setup_process, parse_args, system_info, reconfigure_logging
from resotocore.error import RestartService
from resotocore.message_bus import MessageBus
from resotocore.model.model_handler import ModelHandlerDB
from resotocore.model.typed_model import to_json, class_fqn
from resotocore.query.template_expander_service import TemplateExpanderService
from resotocore.report.inspector_service import InspectorService
from resotocore.task.scheduler import Scheduler
from resotocore.task.subscribers import SubscriptionHandler
from resotocore.task.task_handler import TaskHandlerService
from resotocore.user.user_management import UserManagementService
from resotocore.util import shutdown_process, utc
from resotocore.web.api import Api
from resotocore.web.certificate_handler import CertificateHandler
from resotocore.worker_task_queue import WorkerTaskQueue
from resotocore.infra_apps.local_runtime import LocalResotocoreAppRuntime
from resotocore.infra_apps.package_manager import PackageManager
from resotocore.graph_manager.graph_manager import GraphManager
from resotolib.asynchronous.web import runner

log = logging.getLogger("resotocore")


def main() -> None:
    """
    Application entrypoint - no arguments are allowed.
    """
    try:
        run(sys.argv[1:])
        log.info("Process finished.")
    except (KeyboardInterrupt, SystemExit):
        log.info("Stopping resoto graph core.")
        shutdown_process(0)
    except Exception as ex:
        if "--debug" in sys.argv:
            print(traceback.format_exc())
        print(f"resotocore stopped. Reason {class_fqn(ex)}: {ex}", file=sys.stderr)
        shutdown_process(1)


def run(arguments: List[str]) -> None:
    """
    Run application. When this method returns, the process is done.
    :param arguments: the arguments provided to this process.
                 Note: this method is used in tests to specify arbitrary arguments.
    """
    args = parse_args(arguments)
    setup_process(args)

    # after setup, logging is possible
    info = system_info()
    log.info(
        f"Starting up version={info.version} on system with cpus={info.cpus}, "
        f"available_mem={info.mem_available}, total_mem={info.mem_total}"
    )

    # The loop is here to restart the process in case of RestartService exceptions.
    while True:
        try:
            run_process(args)
            break  # This line should never be reached. In case it does, break the loop.
        except RestartService as ex:
            message = f"Restarting Service. Reason: {ex.reason}"
            line = "-" * len(message)
            print(f"\n{line}\n{message}\n{line}\n")


def run_process(args: Namespace) -> None:
    with TemporaryDirectory() as temp_name:
        temp = Path(temp_name)
        with warnings.catch_warnings():  # ignore ssl errors during setup
            warnings.simplefilter("ignore", HTTPWarning)
            # wait here for an initial connection to the database before we continue. blocking!
            created, system_data, sdb = DbAccess.connect(args, timedelta(seconds=120), verify=False)
            # only to be used for CoreConfig creation
            core_config_override_service = asyncio.run(override_config_for_startup(args.config_override_path))
            config = config_from_db(args, sdb, lambda: core_config_override_service.get_override(ResotoCoreConfigId))
            cert_handler = CertificateHandler.lookup(config, sdb, temp)
            verify: Union[bool, str] = False if args.graphdb_no_ssl_verify else str(cert_handler.ca_bundle)
            config = evolve(config, run=RunConfig(temp, verify))
        # in case of tls: connect again with the correct certificate settings
        use_tls = args.graphdb_server.startswith("https://")
        db_client = DbAccess.connect(args, timedelta(seconds=30), verify=verify)[2] if use_tls else sdb
        with_config(created, system_data, db_client, cert_handler, config, args.config_override_path)


def with_config(
    created: bool,
    system_data: SystemData,
    sdb: StandardDatabase,
    cert_handler: CertificateHandler,
    config: CoreConfig,
    config_overrides_paths: List[Path],
) -> None:
    reconfigure_logging(config)  # based on the config, logging might have changed
    # only lg the editable config - to not log any passwords
    log.debug(f"Starting with config: {config.editable}")
    info = system_info()
    event_sender = PostHogEventSender(system_data) if config.runtime.usage_metrics else NoEventSender()
    db = db_access(config, sdb, event_sender)
    message_bus = MessageBus()
    scheduler = Scheduler()
    worker_task_queue = WorkerTaskQueue()
    model = ModelHandlerDB(db, config.runtime.plantuml_server)
    # a "real" config override service, unlike the one used for core config
    config_override_service = ConfigOverrideService(config_overrides_paths, partial(model_from_db, db.configs_model_db))
    config_handler = ConfigHandlerService(
        db.config_entity_db,
        db.config_validation_entity_db,
        db.configs_model_db,
        worker_task_queue,
        message_bus,
        event_sender,
        config,
        config_override_service,
    )
    user_management = UserManagementService(db, config_handler, event_sender)
    cli_deps = CLIDependencies(
        message_bus=message_bus,
        event_sender=event_sender,
        db_access=db,
        model_handler=model,
        worker_task_queue=worker_task_queue,
        config=config,
        config_handler=config_handler,
        cert_handler=cert_handler,
        user_management=user_management,
    )
    default_env = {"graph": config.cli.default_graph, "section": config.cli.default_section}
    cli = CLIService(cli_deps, all_commands(cli_deps), default_env, alias_names())
    template_expander = TemplateExpanderService(db.template_entity_db, cli)
    cli_deps.extend(template_expander=template_expander)
    inspector = InspectorService(cli)
    subscriptions = SubscriptionHandler(db.subscribers_db, message_bus)
    task_handler = TaskHandlerService(
        db.running_task_db, db.job_db, message_bus, event_sender, subscriptions, scheduler, cli, config
    )
    core_config_handler = CoreConfigHandler(
        config, message_bus, worker_task_queue, config_handler, event_sender, inspector
    )
    merge_outer_edges_handler = MergeOuterEdgesHandler(message_bus, subscriptions, task_handler, db, model)
    cli_deps.extend(task_handler=task_handler, inspector=inspector)
    infra_apps_runtime = LocalResotocoreAppRuntime(cli)
    cli_deps.extend(infra_apps_runtime=infra_apps_runtime)
    infra_apps_package_manager = PackageManager(
        db.package_entity_db, config_handler, cli.register_infra_app_alias, cli.unregister_infra_app_alias
    )
    cli_deps.extend(infra_apps_package_manager=infra_apps_package_manager)
    graph_manager = GraphManager(db, config.snapshots, core_config_handler, task_handler)
    cli_deps.extend(graph_manager=graph_manager)
    api = Api(
        db,
        model,
        subscriptions,
        task_handler,
        message_bus,
        event_sender,
        worker_task_queue,
        cert_handler,
        config_handler,
        inspector,
        cli,
        template_expander,
        config,
        user_management,
        config_override_service.get_override,
        graph_manager,
    )
    event_emitter = emit_recurrent_events(
        event_sender, model, subscriptions, worker_task_queue, message_bus, timedelta(hours=1), timedelta(hours=1)
    )

    async def on_start() -> None:
        # queue must be created inside an async function!
        cli_deps.extend(forked_tasks=Queue())
        await db.start()
        await event_sender.start()
        await subscriptions.start()
        await scheduler.start()
        await worker_task_queue.start()
        await event_emitter.start()
        await cli.start()
        await inspector.start()
        await task_handler.start()
        await config_override_service.start()
        await config_handler.start()
        await core_config_handler.start()
        await merge_outer_edges_handler.start()
        await cert_handler.start()
        await api.start()
        await infra_apps_package_manager.start()
        await graph_manager.start()
        if created:
            docker = inside_docker()
            kubernetes = inside_kubernetes()
            helm = helm_installation()
            await event_sender.core_event(
                CoreEvent.SystemInstalled,
                {
                    "docker_install": docker,
                    "k8s_install": kubernetes,
                    "helm_install": helm,
                    "pip_install": not (docker or kubernetes or helm),
                },
            )
        await event_sender.core_event(
            CoreEvent.SystemStarted,
            {
                "version": version(),
                "created_at": to_json(system_data.created_at),
                "system": platform.system(),
                "platform": platform.platform(),
                "inside_docker": info.inside_docker,
            },
            cpu_count=info.cpus,
            mem_total=info.mem_total,
            mem_available=info.mem_available,
        )

    async def on_stop() -> None:
        duration = utc() - info.started_at
        await graph_manager.stop()
        await infra_apps_package_manager.stop()
        await api.stop()
        await cert_handler.stop()
        await config_override_service.stop()
        await core_config_handler.stop()
        await merge_outer_edges_handler.stop()
        await task_handler.stop()
        await inspector.stop()
        await cli.stop()
        await event_sender.core_event(CoreEvent.SystemStopped, total_seconds=int(duration.total_seconds()))
        await event_emitter.stop()
        await worker_task_queue.stop()
        await scheduler.stop()
        await subscriptions.stop()
        await db.stop()
        await event_sender.stop()

    async def async_initializer() -> Application:
        async def clean_all_tasks() -> None:
            log.info("Clean up all running tasks.")
            for task in asyncio.all_tasks():
                with suppress(asyncio.CancelledError):
                    if not task.done() or not task.cancelled():
                        task.cancel()
                    log.debug(f"Wait for task: {task}")
                    await task

        async def on_start_stop(_: Application) -> AsyncIterator[None]:
            await on_start()
            log.info("Initialization done. Starting API.")
            yield
            log.info("Shutdown initiated. Stop all tasks.")
            await on_stop()
            await clean_all_tasks()

        api.app.cleanup_ctx.append(on_start_stop)
        return api.app

    runner.run_app(
        async_initializer(),
        api.stop,
        host=config.api.web_hosts,
        https_port=config.api.https_port,
        http_port=config.api.http_port,
        default_port=8900,
        ssl_context=cert_handler.host_context,
    )


if __name__ == "__main__":
    main()
