import copy
import inspect
import logging
import re
import warnings
from pathlib import Path
from typing import Any, Callable, List, Optional, Tuple, Union

from runhouse import globals
from runhouse.resources.envs import _get_env_from, Env
from runhouse.resources.hardware import _get_cluster_from, Cluster
from runhouse.resources.module import Module
from runhouse.resources.packages import git_package

from runhouse.resources.resource import Resource

logger = logging.getLogger(__name__)


class Function(Module):
    RESOURCE_TYPE = "function"
    DEFAULT_ACCESS = "write"

    def __init__(
        self,
        fn_pointers: Optional[Tuple] = None,
        name: Optional[str] = None,
        system: Optional[Cluster] = None,
        env: Optional[Env] = None,
        dryrun: bool = False,
        access: Optional[str] = None,
        **kwargs,  # We have this here to ignore extra arguments when calling from from_config
    ):
        """
        Runhouse Function object. It is comprised of the entrypoint, system/cluster,
        and dependencies necessary to run the service.

        .. note::
                To create a Function, please use the factory method :func:`function`.
        """
        self.fn_pointers = fn_pointers
        self.access = access or self.DEFAULT_ACCESS
        super().__init__(name=name, dryrun=dryrun, system=system, env=env, **kwargs)

    # ----------------- Constructor helper methods -----------------

    @classmethod
    def from_config(cls, config: dict, dryrun: bool = False):
        """Create a Function object from a config dictionary."""
        if isinstance(config["system"], dict):
            config["system"] = Cluster.from_config(config["system"], dryrun=dryrun)
        if isinstance(config["env"], dict):
            config["env"] = Env.from_config(config["env"], dryrun=dryrun)

        config.pop("resource_subtype", None)
        return Function(**config, dryrun=dryrun)

    @classmethod
    def _check_for_child_configs(cls, config):
        """Overload by child resources to load any resources they hold internally."""
        # TODO: Replace with _get_cluster_from?
        system = config["system"]
        if isinstance(system, str):
            config["system"] = globals.rns_client.load_config(name=system)
            # if the system is set to a cluster
            if not config["system"]:
                raise Exception(f"No cluster config saved for {system}")

        config["env"] = _get_env_from(config["env"])
        return config

    def to(
        self,
        system: Union[str, Cluster] = None,
        env: Union[List[str], Env] = [],
        # Variables below are deprecated
        reqs: Optional[List[str]] = None,
        setup_cmds: Optional[List[str]] = [],
        force_install: bool = False,
    ):
        """
        Set up a Function and Env on the given system.

        See the args of the factory method :func:`function` for more information.

        Example:
            >>> rh.function(fn=local_fn).to(gpu_cluster)
            >>> rh.function(fn=local_fn).to(system=gpu_cluster, env=my_conda_env)
        """
        if setup_cmds:
            warnings.warn(
                "``setup_cmds`` argument has been deprecated. "
                "Please pass in setup commands to the ``Env`` class corresponding to the function instead."
            )

        # to retain backwards compatibility
        if reqs or setup_cmds:
            warnings.warn(
                "``reqs`` and ``setup_cmds`` arguments has been deprecated. Please use ``env`` instead."
            )
            env = Env(reqs=reqs, setup_cmds=setup_cmds, name=Env.DEFAULT_NAME)
        elif env and isinstance(env, List):
            env = Env(reqs=env, setup_cmds=setup_cmds, name=Env.DEFAULT_NAME)
        else:
            env = env or self.env or Env(name=Env.DEFAULT_NAME)
            env = _get_env_from(env)

        if (
            self.dryrun
            or not (system or self.system)
            or self.access not in ["write", "read"]
        ):
            # don't move the function to a system
            self.env = env
            return self

        # We need to backup the system here so the __getstate__ method of the cluster
        # doesn't wipe the client of this function's cluster when deepcopy copies it.
        hw_backup = self.system
        self.system = None
        new_function = copy.deepcopy(self)
        self.system = hw_backup

        new_function.system = (
            _get_cluster_from(system, dryrun=self.dryrun) if system else self.system
        )

        logging.info("Setting up Function on cluster.")
        # To up cluster in case it's not yet up
        new_function.system.check_server()
        new_function.name = new_function.name or self.fn_pointers[2]
        # TODO
        # env.name = env.name or (new_function.name + "_env")
        new_env = env.to(new_function.system, force_install=force_install)
        new_function.env = new_env

        new_function.dryrun = True
        system.put_resource(new_function, dryrun=True)
        logging.info("Function setup complete.")

        return new_function

    # ----------------- Function call methods -----------------

    def __call__(self, *args, **kwargs) -> Any:
        """Call the function on its system

        Args:
             *args: Optional args for the Function
             stream_logs (bool): Whether to stream the logs from the Function's execution.
                Defaults to ``True``.
             run_name (Optional[str]): Name of the Run to create. If provided, a Run will be created
                for this function call, which will be executed synchronously on the cluster before returning its result
             **kwargs: Optional kwargs for the Function

        Returns:
            The Function's return value
        """
        return self.call(*args, **kwargs)

    def call(self, *args, **kwargs) -> Any:
        # We need this strictly because Module's __getattribute__ overload can't pick up the __call__ method
        fn = self._get_obj_from_pointers(*self.fn_pointers)
        return fn(*args, **kwargs)

    @property
    def _is_async(self) -> Any:
        if not self.fn_pointers:
            return False
        try:
            fn = self._get_obj_from_pointers(*self.fn_pointers)
        except ModuleNotFoundError:
            return False
        if not fn:
            return False
        return inspect.iscoroutinefunction(fn) or inspect.isasyncgenfunction(fn)

    @property
    def _is_async_gen(self) -> Any:
        if not self.fn_pointers:
            return False
        try:
            fn = self._get_obj_from_pointers(*self.fn_pointers)
        except ModuleNotFoundError:
            return False
        if not fn:
            return False
        return inspect.isasyncgenfunction(fn)

    def map(self, *args, **kwargs):
        """Map a function over a list of arguments.

        Example:
            >>> def local_sum(arg1, arg2, arg3):
            >>>     return arg1 + arg2 + arg3
            >>>
            >>> remote_fn = rh.function(local_fn).to(gpu)
            >>> remote_fn.map([1, 2], [1, 4], [2, 3])
            >>> # output: [4, 9]

        """
        import ray

        fn = self._get_obj_from_pointers(*self.fn_pointers)
        ray_wrapped_fn = ray.remote(fn)
        return ray.get([ray_wrapped_fn.remote(*args, **kwargs) for args in zip(*args)])

    def starmap(self, args_lists, **kwargs):
        """Like :func:`map` except that the elements of the iterable are expected to be iterables
        that are unpacked as arguments. An iterable of [(1,2), (3, 4)] results in [func(1,2), func(3,4)].

        Example:
            >>> arg_list = [(1,2), (3, 4)]
            >>> # runs the function twice, once with args (1, 2) and once with args (3, 4)
            >>> remote_fn.starmap(arg_list)
        """
        import ray

        fn = self._get_obj_from_pointers(*self.fn_pointers)
        ray_wrapped_fn = ray.remote(fn)
        return ray.get([ray_wrapped_fn.remote(*args, **kwargs) for args in args_lists])

    def remote(self, *args, local=True, **kwargs):
        obj = self.call.remote(*args, **kwargs)
        return obj

    def run(self, *args, local=True, **kwargs):
        key = self.call.run(*args, **kwargs)
        return key

    def get(self, run_key):
        """Get the result of a Function call that was submitted as async using `run`.

        Args:
            run_key: A single or list of runhouse run_key strings returned by a Function.remote() call. The ObjectRefs
                must be from the cluster that this Function is running on.

        Example:
            >>> remote_fn = rh.function(local_fn).to(gpu)
            >>> remote_fn_run = remote_fn.run()
            >>> remote_fn.get(remote_fn_run.name)
        """
        return self.system.get(run_key)

    @property
    def config_for_rns(self):
        config = super().config_for_rns
        config.update(
            {
                "fn_pointers": self.fn_pointers,
            }
        )
        return config

    def send_secrets(self, providers: Optional[List[str]] = None):
        """Send secrets to the system.

        Example:
            >>> remote_fn.send_secrets(providers=["aws", "lambda"])
        """
        self.system.sync_secrets(providers=providers)

    def http_url(self, curl_command=False, *args, **kwargs) -> str:
        """
        Return the endpoint needed to run the Function on the remote cluster, or provide the curl command if requested.
        """
        raise NotImplementedError("http_url not yet implemented for Function")

    def notebook(self, persist=False, sync_package_on_close=None, port_forward=8888):
        """Tunnel into and launch notebook from the system."""
        # Roughly trying to follow:
        # https://towardsdatascience.com/using-jupyter-notebook-running-on-a-remote-docker-container-via-ssh-ea2c3ebb9055
        # https://docs.ray.io/en/latest/ray-core/using-ray-with-jupyter.html
        if self.system is None:
            raise RuntimeError("Cannot SSH, running locally")

        tunnel, port_fwd = self.system.ssh_tunnel(
            local_port=port_forward, num_ports_to_try=10
        )
        try:
            install_cmd = "pip install jupyterlab"
            jupyter_cmd = f"jupyter lab --port {port_fwd} --no-browser"
            # port_fwd = '-L localhost:8888:localhost:8888 '  # TOOD may need when we add docker support
            with self.system.pause_autostop():
                self.system.run(commands=[install_cmd, jupyter_cmd], stream_logs=True)

        finally:
            if sync_package_on_close:
                if sync_package_on_close == "./":
                    sync_package_on_close = globals.rns_client.locate_working_dir()
                from .folders import folder

                folder(system=self.system, path=sync_package_on_close).to("here")
            if not persist:
                tunnel.stop()
                kill_jupyter_cmd = f"jupyter notebook stop {port_fwd}"
                self.system.run(commands=[kill_jupyter_cmd])

    def get_or_call(self, run_name: str, load=True, local=True, *args, **kwargs) -> Any:
        """Check if object already exists on cluster or rns, and if so return the result. If not, run the function.
        Keep in mind this can be called with any of the usual method call modifiers - `remote=True`, `run_async=True`,
        `stream_logs=False`, etc.

        Args:
            run_name (Optional[str]): Name of a particular run for this function.
                If not provided will use the function's name.
            load (bool): Whether to load the name from the RNS if it exists.
            *args: Arguments to pass to the function for the run (relevant if creating a new run).
            **kwargs: Keyword arguments to pass to the function for the run (relevant if creating a new run).

        Returns:
            Any: Result of the Run

        Example:
            >>> # previously, remote_fn.run(arg1, arg2, run_name="my_async_run")
            >>> remote_fn.get_or_call()
        """
        # TODO let's just do this for functions initially, and decide if we want to support it for calls on modules
        #  as well. Right now this only works with remote=True, we should decide if we want to fix that later.
        if load:
            resource = globals.rns_client.load_config(name=run_name)
            if resource:
                return Resource.from_name(name=run_name, dryrun=self.dryrun)
        try:
            return self.system.get(run_name, default=KeyError, remote=True)
        except KeyError:
            logger.info(f"Item {run_name} not found on cluster. Running function.")

        return self.call(*args, **kwargs, run_name=run_name, remote=True)

    def keep_warm(
        self,
        autostop_mins=None,
    ):
        """Keep the system warm for autostop_mins. If autostop_mins is ``None`` or -1, keep warm indefinitely.

        Example:
            >>> # keep gpu warm for 30 mins
            >>> remote_fn = rh.function(local_fn).to(gpu)
            >>> remote_fn.keep_warm(autostop_mins=30)
        """
        if autostop_mins is None:
            logger.info(f"Keeping {self.name} indefinitely warm")
            # keep indefinitely warm if user doesn't specify
            autostop_mins = -1
        self.system.keep_warm(autostop_mins=autostop_mins)

        return self

    @staticmethod
    def _handle_nb_fn(fn, fn_pointers, serialize_notebook_fn, name):
        """Handle the case where the user passes in a notebook function"""
        if serialize_notebook_fn:
            # This will all be cloudpickled by the RPC client and unpickled by the RPC server
            # Note that this means the function cannot be saved, and it's better that way because
            # pickling functions is not meant for long term storage. Case in point, this method will be
            # sensitive to differences in minor Python versions between the serializing and deserializing envs.
            return "", "notebook", fn
        else:
            module_path = Path.cwd() / (f"{name}_fn.py" if name else "sent_fn.py")
            logging.info(
                f"Writing out function to {str(module_path)}. Please make "
                f"sure the function does not rely on any local variables, "
                f"including imports (which should be moved inside the function body)."
            )
            if not name:
                logging.warning(
                    "You should name Functions that are created in notebooks to avoid naming collisions "
                    "between the modules that are created to hold their functions "
                    '(i.e. "sent_fn.py" errors.'
                )
            source = inspect.getsource(fn).strip()
            with module_path.open("w") as f:
                f.write(source)
            return fn_pointers[0], module_path.stem, fn_pointers[2]


def function(
    fn: Optional[Union[str, Callable]] = None,
    name: Optional[str] = None,
    system: Optional[Union[str, Cluster]] = None,
    env: Optional[Union[List[str], Env, str]] = None,
    dryrun: bool = False,
    load_secrets: bool = False,
    serialize_notebook_fn: bool = False,
    # args below are deprecated
    reqs: Optional[List[str]] = None,
    setup_cmds: Optional[List[str]] = None,
):
    """Builds an instance of :class:`Function`.

    Args:
        fn (Optional[str or Callable]): The function to execute on the remote system when the function is called.
        name (Optional[str]): Name of the Function to create or retrieve.
            This can be either from a local config or from the RNS.
        system (Optional[str or Cluster]): Hardware (cluster) on which to execute the Function.
            This can be either the string name of a Cluster object, or a Cluster object.
        env (Optional[List[str] or Env or str]): List of requirements to install on the remote cluster, or path to the
            requirements.txt file, or Env object or string name of an Env object.
        dryrun (bool): Whether to create the Function if it doesn't exist, or load the Function object as a dryrun.
            (Default: ``False``)
        load_secrets (bool): Whether or not to send secrets; only applicable if `dryrun` is set to ``False``.
            (Default: ``False``)
        serialize_notebook_fn (bool): If function is of a notebook setting, whether or not to serialized the function.
            (Default: ``False``)

    Returns:
        Function: The resulting Function object.

    Example:
        >>> import runhouse as rh

        >>> cluster = rh.ondemand_cluster(name="my_cluster")
        >>> def sum(a, b):
        >>>    return a + b

        >>> summer = rh.function(fn=sum, name="my_func").to(cluster, env=['requirements.txt']).save()

        >>> # using the function
        >>> res = summer(5, 8)  # returns 13

        >>> # Load function from above
        >>> reloaded_function = rh.function(name="my_func")
    """
    if name and not any([fn, system, env]):
        # Try reloading existing function
        return Function.from_name(name, dryrun)

    if setup_cmds:
        warnings.warn(
            "``setup_cmds`` argument has been deprecated. "
            "Please pass in setup commands to rh.Env corresponding to the function instead."
        )
    if reqs is not None:
        warnings.warn(
            "``reqs`` argument has been deprecated. Please use ``env`` instead."
        )
        env = Env(
            reqs=reqs, setup_cmds=setup_cmds, working_dir="./", name=Env.DEFAULT_NAME
        )
    elif not isinstance(env, Env):
        env = _get_env_from(env) or Env(working_dir="./", name=Env.DEFAULT_NAME)

    fn_pointers = None
    if callable(fn):
        fn_pointers = Function._extract_pointers(fn, reqs=env.reqs)
        if fn_pointers[1] == "notebook":
            fn_pointers = Function._handle_nb_fn(
                fn,
                fn_pointers=fn_pointers,
                serialize_notebook_fn=serialize_notebook_fn,
                name=fn_pointers[2] or name,
            )
    elif isinstance(fn, str):
        # Url must match a regex of the form
        # 'https://github.com/username/repo_name/blob/branch_name/path/to/file.py:func_name'
        # Use a regex to extract username, repo_name, branch_name, path/to/file.py, and func_name
        pattern = (
            r"https://github\.com/(?P<username>[^/]+)/(?P<repo_name>[^/]+)/blob/"
            r"(?P<branch_name>[^/]+)/(?P<path>[^:]+):(?P<func_name>.+)"
        )
        match = re.match(pattern, fn)

        if match:
            username = match.group("username")
            repo_name = match.group("repo_name")
            branch_name = match.group("branch_name")
            path = match.group("path")
            func_name = match.group("func_name")
        else:
            raise ValueError(
                "fn must be a callable or string of the form "
                '"https://github.com/username/repo_name/blob/branch_name/path/to/file.py:func_name"'
            )
        module_name = Path(path).stem
        relative_path = str(repo_name / Path(path).parent)
        fn_pointers = (relative_path, module_name, func_name)
        # TODO [DG] check if the user already added this in their reqs
        repo_package = git_package(
            git_url=f"https://github.com/{username}/{repo_name}.git",
            revision=branch_name,
        )
        env.reqs = [repo_package] + env.reqs

    system = _get_cluster_from(system)

    new_function = Function(
        fn_pointers=fn_pointers,
        access=Function.DEFAULT_ACCESS,
        name=name,
        dryrun=dryrun,
    ).to(system=system, env=env)

    if load_secrets and not dryrun:
        new_function.send_secrets()

    return new_function
