import codecs
import inspect
import logging
import signal
import threading
import time
import traceback
from pathlib import Path
from typing import List, Union

import ray
import ray.cloudpickle as pickle
from sky.skylet.autostop_lib import set_last_active_time_to_now

from runhouse.rh_config import configs, obj_store

from runhouse.rns.blobs import blob, Blob
from runhouse.rns.module import Module
from runhouse.rns.queues import Queue
from runhouse.rns.resource import Resource
from runhouse.rns.run import run, RunStatus
from runhouse.rns.run_module_utils import call_fn_by_type
from runhouse.rns.utils.names import _generate_default_name
from runhouse.servers.http.http_utils import (
    b64_unpickle,
    Message,
    OutputType,
    pickle_b64,
    Response,
)

logger = logging.getLogger(__name__)


class EnvServlet:
    DEFAULT_PORT = 50052
    MAX_MESSAGE_LENGTH = 1 * 1024 * 1024 * 1024  # 1 GB
    LOGGING_WAIT_TIME = 1.0
    SKY_YAML = str(Path("~/.sky/sky_ray.yml").expanduser())
    LOGS_DIR = ".rh/logs"
    RH_LOGFILE_PATH = Path.home() / LOGS_DIR

    def __init__(self, env_name, *args, **kwargs):
        self.env_name = env_name

        obj_store.set_name(self.env_name)

        self.output_types = {}
        self.thread_ids = {}

    @staticmethod
    def register_activity():
        set_last_active_time_to_now()

    def put_resource(self, message: Message):
        self.register_activity()
        try:
            resource_config, state, dryrun = b64_unpickle(message.data)
            # Resolve any sub-resources which are string references to resources already sent to this cluster.
            # We need to pop the resource's own name so it doesn't get resolved if it's already present in the
            # obj_store.
            name = resource_config.pop("name")
            subtype = resource_config.pop("resource_subtype")
            resource_config = obj_store.get_obj_refs_dict(resource_config)
            resource_config["name"] = name
            resource_config["resource_subtype"] = subtype

            logger.info(
                f"Message received from client to construct resource: {resource_config}"
            )

            resource = Resource.from_config(config=resource_config, dryrun=dryrun)

            for attr, val in state.items():
                setattr(resource, attr, val)

            name = (
                resource.name
                or message.key
                or _generate_default_name(prefix=resource.RESOURCE_TYPE)
            )
            if isinstance(resource, Module):
                resource.rename(name)
            else:
                resource.name = name
            obj_store.put(resource.name, resource)

            if hasattr(resource, "remote_init"):
                logger.info(
                    f"Initializing module {resource.name} in env servlet {self.env_name}"
                )
                resource.remote_init()

            self.register_activity()
            # Return the name in case we had to set it
            return Response(output_type=OutputType.RESULT, data=pickle_b64(name))
        except Exception as e:
            logger.exception(e)
            self.register_activity()
            return Response(
                error=pickle_b64(e),
                traceback=pickle_b64(traceback.format_exc()),
                output_type=OutputType.EXCEPTION,
            )

    def call_module_method(self, module_name, method_name, message: Message):
        self.register_activity()
        result_resource = None

        persist = message.save or message.remote or message.run_async
        try:
            logger.info(
                f"Message received from client to call method {method_name} on module {module_name} at {time.time()}"
            )

            self.thread_ids[
                message.key
            ] = threading.get_ident()  # Save thread ID for this message
            # Remove output types from previous runs
            self.output_types.pop(message.key, None)
            result_resource = Queue(name=message.key, persist=persist)
            result_resource.pin()
            result_resource.provenance = run(name=message.key, load=False)
            result_resource.provenance.__enter__()

            # Save now so status and initial streamed results are available globally
            if message.save:
                result_resource.save()

            args, kwargs = b64_unpickle(message.data) if message.data else ([], {})
            module = obj_store.get(module_name, None)
            if not module:
                raise ValueError(f"Resource {module_name} not found")

            # If method_name is None, return the module itself as this is a "get" request
            try:
                method = getattr(module, method_name) if method_name else module
            except AttributeError:
                logger.debug(module.__dict__)
                raise ValueError(
                    f"Method {method_name} not found on module {module_name}"
                )

            # Don't call the method if it's a property or a "get" request (returning the module itself)
            if hasattr(method, "__call__") and method_name:
                # If method is callable, call it and return the result
                logger.info(
                    f"{self.env_name} servlet: Calling method {method_name} on module {module_name}"
                )
                callable_method = True
            else:
                # Method is a property, return the value
                logger.info(
                    f"Env {self.env_name} servlet: Getting property {method_name} on module {module_name}"
                )
                callable_method = False

            if not callable_method and kwargs and "new_value" in kwargs:
                # If new_value was passed, that means we're setting a property
                setattr(module, method_name, kwargs["new_value"])
                # module.pin()
                self.output_types[message.key] = OutputType.SUCCESS
                result_resource.provenance.__exit__(None, None, None)
                return

            # If method is a property, `method = getattr(module, method_name, None)` above already
            # got our result
            result = method(*args, **kwargs) if callable_method else method
            if inspect.isgenerator(result):
                # Stream back the results of the generator
                logger.info(
                    f"Streaming back results of generator {module_name}.{method_name}"
                )
                self.output_types[message.key] = OutputType.RESULT_STREAM
                for val in result:
                    self.register_activity()
                    # Doing this at the top of the loop so we can catch the final result and change the OutputType
                    result_resource.put(val)

                # Set run status to COMPLETED to indicate end of stream
                result_resource.provenance.__exit__(None, None, None)

                # Resave with new status
                if message.save:
                    result_resource.save()
            else:
                # If the user needs this result again later, don't put it in queue or
                # it will be gone after the first get
                if persist:
                    if isinstance(result, Resource):
                        # If the user's method returned a resource, save that resource as the result
                        # instead of the queue so it's available for global caching
                        result.provenance = result_resource.provenance
                        result.name = message.key
                        result_resource = result
                    else:
                        # We shouldn't return a queue if the result is not a generator, so replace it with a blob
                        result_resource = Blob(
                            name=message.key, provenance=result_resource.provenance
                        )
                        result_resource.data = result

                    result_resource.pin()

                    # Write out the new result_resource to the obj_store
                    # obj_store.put(message.key, result_resource, env=self.env_name)
                else:
                    # Put the result in the queue so we can retrieve it once
                    result_resource.put(result)

                # If not a generator, the method was already called above and completed
                self.output_types[message.key] = OutputType.RESULT
                result_resource.provenance.__exit__(None, None, None)

                if message.save:
                    result_resource.save()
                self.register_activity()
        except Exception as e:
            logger.exception(e)
            self.register_activity()

            # Setting this here is great because it allows us to still return all the computed values of a
            # generator before hitting the exception, stream the logs back to the client until raising the exception,
            # and indicate that we hit an exception before any results are available if that's the case.
            self.output_types[message.key] = OutputType.EXCEPTION
            result_resource.provenance.__exit__(
                type(e), e, traceback.format_exc()
            )  # TODO use format_tb instead?

    def run_module(self, message: Message):
        self.register_activity()
        # get the function result from the incoming request
        [
            relative_path,
            module_name,
            fn_name,
            fn_type,
            resources,
            conda_env,
            env_vars,
            run_name,
            args,
            kwargs,
        ] = b64_unpickle(message.data)

        try:
            args = obj_store.get_obj_refs_list(args)
            kwargs = obj_store.get_obj_refs_dict(kwargs)

            result = call_fn_by_type(
                fn_type=fn_type,
                fn_name=fn_name,
                relative_path=relative_path,
                module_name=module_name,
                resources=resources,
                conda_env=conda_env,
                env_vars=env_vars,
                run_name=run_name,
                args=args,
                kwargs=kwargs,
                serialize_res=True,
            )
            # We need to pin the run_key in the server's Python context rather than inside the call_fn context,
            # because the call_fn context is a separate process and the pinned object will be lost when Ray
            # garbage collects the call_fn process.
            from runhouse import Run

            (res, obj_ref, run_key) = result

            if obj_ref is not None:
                obj_store.put_obj_ref(key=run_key, obj_ref=obj_ref)

            result = pickle.dumps(res) if isinstance(res, Run) else res

            self.register_activity()
            if isinstance(result, ray.exceptions.RayTaskError):
                # If Ray throws an error when executing the function as part of a Run,
                # it will be reflected in the result since we catch the exception and do not immediately raise it
                logger.exception(result)
                return Response(
                    error=pickle_b64(result),
                    traceback=pickle_b64(traceback.format_exc()),
                    output_type=OutputType.EXCEPTION,
                )
            elif isinstance(result, list):
                return Response(
                    data=[codecs.encode(i, "base64").decode() for i in result],
                    output_type=OutputType.RESULT_LIST,
                )
            else:
                return Response(
                    data=codecs.encode(result, "base64").decode(),
                    output_type=OutputType.RESULT,
                )
        except Exception as e:
            logger.exception(e)
            self.register_activity()
            return Response(
                error=pickle_b64(e),
                traceback=pickle_b64(traceback.format_exc()),
                output_type=OutputType.EXCEPTION,
            )

    def get(self, key, remote=False, stream=False, timeout=None, _intra_cluster=False):
        """Get an object from the servlet's object store.

        Args:
            key (str): The key of the object to get.
            remote (bool): Whether to return the object or it's config to construct a remote object.
            stream (bool): Whether to stream results as available (if the key points to a queue).
        """
        self.register_activity()
        try:
            if not obj_store.contains(key):
                return Response(output_type=OutputType.NOT_FOUND, data=key)

            ret_obj = obj_store.get(
                key, timeout=timeout, check_other_envs=not _intra_cluster
            )
            logger.debug(
                f"Servlet {self.env_name} got object of type "
                f"{type(ret_obj)} back from object store for key {key}"
            )
            if _intra_cluster:
                if remote:
                    return ret_obj.config_for_rns
                return ret_obj

            # If the request doesn't want a stream, we can just return the queue object in same way as any other, below
            if isinstance(ret_obj, Queue) and stream:
                if remote and self.output_types.get(key) in [
                    OutputType.RESULT_STREAM,
                    OutputType.SUCCESS_STREAM,
                ]:
                    # If this is a "remote" request and we already know the output type is a stream, we can
                    # return the Queue as a remote immediately so the client can start streaming the results
                    res = ret_obj.config_for_rns
                    res["dryrun"] = True
                    return Response(
                        data=res,
                        output_type=OutputType.CONFIG,
                    )

                # If we're waiting for a result, this will block until one is available, which will either
                # cause the server's ray.get to timeout so it can try again, or return None as soon as the result is
                # available so the server can try requesting again now that it's ready.
                if ret_obj.empty():
                    if (
                        not ret_obj.provenance
                        or ret_obj.provenance.status == RunStatus.NOT_STARTED
                    ):
                        while key not in self.output_types:
                            time.sleep(0.1)
                        return

                    # This allows us to return the results of a generator as they become available, rather than
                    # waiting a full second for the ray.get in the server to timeout before trying again.
                    if ret_obj.provenance.status == RunStatus.RUNNING:
                        # while (
                        #     isinstance(ret_obj, Queue)
                        #     and ret_obj.empty()
                        #     and ret_obj.provenance.status == RunStatus.RUNNING
                        # ):
                        #     ret_obj = ret_obj.refresh()
                        time.sleep(0.1)
                        return

                    if ret_obj.provenance.status == RunStatus.COMPLETED:
                        # We need to look up the output type because this could be a stream with no results left,
                        # which should still return OutputType.RESULT_STREAM, or a call with no result, which should
                        # return OutputType.SUCCESS
                        if self.output_types[key] == OutputType.RESULT_STREAM:
                            return Response(output_type=OutputType.SUCCESS_STREAM)
                        else:
                            return Response(output_type=OutputType.SUCCESS)

                    if ret_obj.provenance.status == RunStatus.ERROR:
                        return Response(
                            error=pickle_b64(ret_obj.provenance.error),
                            traceback=pickle_b64(ret_obj.provenance.traceback),
                            output_type=OutputType.EXCEPTION,
                        )

                    if ret_obj.provenance.status == RunStatus.CANCELLED:
                        return Response(output_type=OutputType.CANCELLED)

                res = ret_obj.get(block=True, timeout=timeout)
                # There's no OutputType.EXCEPTION case to handle here, because if an exception were thrown the
                # provenance.status would be RunStatus.ERROR, and we want to continue retrieving results until the
                # queue is empty, and then will return the exception and traceback in the empty case above.
                return Response(
                    data=pickle_b64(res),
                    output_type=self.output_types[key],
                )

            # If the user requests a remote object, we can return a queue before results complete so they can
            # stream in results directly from the queue. For all other cases, we need to wait for the results
            # to be available.
            if remote:
                if not isinstance(ret_obj, Resource):
                    # If the user requests a remote of an object that is not a Resource, we need to wrap it
                    # in a Resource first, which will overwrite the original object in the object store. We
                    # may want to just throw an error instead, but let's see if this is acceptable to start.
                    ret_obj = blob(data=ret_obj, name=key)
                    ret_obj.pin()

                if ret_obj.provenance and ret_obj.provenance.status == RunStatus.ERROR:
                    return Response(
                        error=pickle_b64(ret_obj.provenance.error),
                        traceback=pickle_b64(ret_obj.provenance.traceback),
                        output_type=OutputType.EXCEPTION,
                    )

                # If this is a "remote" request, just return the rns config and the client will reconstruct the
                # resource from it
                res = ret_obj.config_for_rns
                res["dryrun"] = True
                return Response(
                    data=res,
                    output_type=OutputType.CONFIG,
                )

            if isinstance(ret_obj, Resource) and ret_obj.provenance:
                if ret_obj.provenance.status == RunStatus.ERROR:
                    return Response(
                        error=pickle_b64(ret_obj.provenance.error),
                        traceback=pickle_b64(ret_obj.provenance.traceback),
                        output_type=OutputType.EXCEPTION,
                    )
                # Includes the case where the user called a method with remote or save, where even if the original
                # return value wasn't a resource, we want to return the wrapped resource anyway. If the user called
                # a non-generator method without remote or save, the result would be in a queue and handled above,
                # so it'll still be returned unwrapped.
                if ret_obj.provenance.status == RunStatus.COMPLETED:
                    if isinstance(ret_obj, Blob) and not remote:
                        # If the user doesn't want the remote object, we need to return the actual data
                        ret_obj = ret_obj.data

                    return Response(
                        data=pickle_b64(ret_obj),
                        output_type=OutputType.RESULT,
                    )

                if ret_obj.provenance.status == RunStatus.CANCELLED:
                    return Response(output_type=OutputType.CANCELLED)

                # We don't need to handle the ret_obj.provenance.status == RunStatus.NOT_STARTED case, because
                # if the run hasn't started yet, the result_resource will still be a Queue and handled above.
                # If the run has started, but for some reason the Queue hasn't been created yet (even though it's
                # created immediately), the ret_obj wouldn't be found in the obj_store.

            return Response(
                data=pickle_b64(ret_obj),
                output_type=OutputType.RESULT,
            )
        except Exception as e:
            return Response(
                error=pickle_b64(e),
                traceback=pickle_b64(traceback.format_exc()),
                output_type=OutputType.EXCEPTION,
            )

    def get_logfiles(self, key):
        return obj_store.get_logfiles(key)

    def put_object(self, key, value, _intra_cluster=False):
        self.register_activity()
        # We may not want to deserialize the object here in case the object requires dependencies
        # (to be used inside an env) which aren't present in the BaseEnv.
        if _intra_cluster:
            obj = value
        else:
            obj = b64_unpickle(value)
        logger.info(f"Message received from client to get object: {key}")
        try:
            obj_store.put(key, obj)
            return Response(output_type=OutputType.SUCCESS)
        except Exception as e:
            logger.exception(e)
            self.register_activity()
            return Response(
                error=pickle_b64(e),
                traceback=pickle_b64(traceback.format_exc()),
                output_type=OutputType.EXCEPTION,
            )

    def rename_object(self, message: Message):
        self.register_activity()
        # We may not want to deserialize the object here in case the object requires dependencies
        # (to be used inside an env) which aren't present in the BaseEnv.
        old_key, new_key = b64_unpickle(message.data)
        logger.info(
            f"Message received from client to rename object {old_key} to {new_key}"
        )
        try:
            obj_store.rename(old_key, new_key)
            return Response(output_type=OutputType.SUCCESS)
        except Exception as e:
            logger.exception(e)
            self.register_activity()
            return Response(
                error=pickle_b64(e),
                traceback=pickle_b64(traceback.format_exc()),
                output_type=OutputType.EXCEPTION,
            )

    def delete_obj(self, message: Union[Message, List], _intra_cluster=False):
        self.register_activity()
        keys = b64_unpickle(message.data) if not _intra_cluster else message
        logger.info(f"Message received from client to delete keys: {keys or 'all'}")
        try:
            cleared = []
            if keys:
                for pin in keys:
                    obj_store.delete(pin)
                    cleared.append(pin)
            else:
                cleared = list(obj_store.keys())
                obj_store.clear()
            return Response(data=pickle_b64(cleared), output_type=OutputType.RESULT)
        except Exception as e:
            logger.exception(e)
            self.register_activity()
            return Response(
                error=pickle_b64(e),
                traceback=pickle_b64(traceback.format_exc()),
                output_type=OutputType.EXCEPTION,
            )

    def cancel_run(self, message: Message):
        # Having this be a POST instead of a DELETE on the "run" endpoint is strange, but we're not actually
        # deleting the run, just cancelling it. Maybe we should merge this into get_object to allow streaming logs.
        self.register_activity()
        force = b64_unpickle(message.data)
        logger.info(f"Message received from client to cancel runs: {message.key}")

        def kill_thread(key, sigterm=False):
            thread_id = self.thread_ids.get(key)
            if not thread_id:
                return
            # Get thread object from id
            # print(list(threading.enumerate()))
            # print(self.thread_ids.items())
            thread = threading._active.get(thread_id)
            if thread is None:
                return
            if thread.is_alive():
                # exc = KeyboardInterrupt()
                # thread._async_raise(exc)
                logging.info(f"Killing thread {thread_id}")
                # SIGINT is like Ctrl+C: https://docs.python.org/3/library/signal.html#signal.SIGINT
                signal.pthread_kill(
                    thread_id, signal.SIGINT if not sigterm else signal.SIGTERM
                )
                self.output_types[thread_id] = OutputType.CANCELLED
                if obj_store.contains(key):
                    obj = obj_store.get(key)
                    obj.provenance.status = RunStatus.CANCELLED
            self.thread_ids.pop(thread_id, None)

        try:
            if message.key == "all":
                for key in self.thread_ids:
                    kill_thread(key, force)
            else:
                kill_thread(message.key, force)

            return Response(output_type=OutputType.SUCCESS)
        except Exception as e:
            logger.exception(e)
            self.register_activity()
            return Response(
                error=pickle_b64(e),
                traceback=pickle_b64(traceback.format_exc()),
                output_type=OutputType.EXCEPTION,
            )

    def get_keys(self):
        self.register_activity()
        keys: list = list(obj_store.keys())
        return Response(data=pickle_b64(keys), output_type=OutputType.RESULT)

    def add_secrets(self, message: Message):
        from runhouse import Secrets

        self.register_activity()
        secrets_to_add: dict = b64_unpickle(message.data)
        failed_providers = (
            {}
        )  # Track which providers fail and send them back to the user
        try:
            for provider_name, provider_secrets in secrets_to_add.items():
                p = Secrets.builtin_provider_class_from_name(provider_name)
                if p is None:
                    error_msg = f"{provider_name} is not a Runhouse builtin provider"
                    failed_providers[provider_name] = error_msg
                    continue

                # NOTE: For now we are always saving in the provider's default location on the cluster
                credentials_path = p.default_credentials_path()
                try:
                    p.save_secrets(provider_secrets, overwrite=True)
                except Exception as e:
                    failed_providers[provider_name] = str(e)
                    continue

                # update config on the cluster with the default creds path for each provider
                configs.set_nested("secrets", {provider_name: credentials_path})
                logger.info(f"Added secrets for {provider_name} to: {credentials_path}")
            return Response(
                data=pickle_b64(failed_providers), output_type=OutputType.RESULT
            )
        except Exception as e:
            logger.exception(e)
            self.register_activity()
            return Response(
                error=pickle_b64(e),
                traceback=pickle_b64(traceback.format_exc()),
                output_type=OutputType.EXCEPTION,
            )

    def call_fn(fn_name, args):
        self.register_activity()
        from runhouse import function

        fn = function(name=fn_name, dryrun=True)
        result = fn(*(args.args or []), **(args.kwargs or {}))

        (fn_res, obj_ref, run_key) = result
        if isinstance(fn_res, bytes):
            fn_res = pickle.loads(fn_res)

        return fn_res
