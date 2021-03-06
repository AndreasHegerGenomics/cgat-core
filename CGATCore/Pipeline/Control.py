"""Control.py - Command line control for ruffus pipelines
=========================================================

The functions :func:`write_config_files`, :func:`clean`,
:func:`clone_pipeline` and :func:`peek_parameters` provide the
functionality for particular pipeline commands.

:class:`MultiLineFormatter` improves the formatting
of long log messages, while

Reference
---------

"""

from io import StringIO
from multiprocessing.pool import ThreadPool
import multiprocessing
import contextlib
import json
import logging
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import gevent.pool
import gevent.queue
from ruffus.task import _pipeline_prepare_to_run, topologically_sorted_nodes, \
    is_node_up_to_date, t_verbose_logger

import ruffus

# talking to a cluster
try:
    import drmaa
    HAS_DRMAA = True
except ImportError:
    HAS_DRMAA = False
except RuntimeError:
    HAS_DRMAA = False

import CGATCore.Experiment as E
import CGATCore.IOTools as IOTools
import CGATCore.Requirements as Requirements
from CGATCore.Pipeline.Parameters import input_validation
from CGATCore.Pipeline.Utils import get_caller, get_caller_locals
from CGATCore.Pipeline.Execution import execute, start_session,\
    close_session


# redirect os.stat and other OS utilities to cached versions to speed
# up ruffus. Be careful not to use os.stat in task functions.
# Does ruffus rely on os.stat for completed tasks?
SAVED_OS_STAT = os.stat
SAVED_OS_PATH_ABSPATH = os.path.abspath
SAVED_OS_PATH_REALPATH = os.path.realpath
SAVED_OS_PATH_RELPATH = os.path.relpath
SAVED_OS_PATH_ISLINK = os.path.islink


@E.cached_function
def cached_os_stat(filename, *args, **kwargs):
    return SAVED_OS_STAT(filename, *args, **kwargs)


@E.cached_function
def cached_os_path_abspath(filename):
    return SAVED_OS_PATH_ABSPATH(filename)


@E.cached_function
def cached_os_path_realpath(filename):
    return SAVED_OS_PATH_REALPATH(filename)


@E.cached_function
def cached_os_path_relpath(filename):
    return SAVED_OS_PATH_RELPATH(filename)


@E.cached_function
def cached_os_path_islink(filename):
    return SAVED_OS_PATH_ISLINK(filename)


# Set from Pipeline.py
PARAMS = {}

# global options and arguments - set but currently not
# used as relevant sections are entered into the PARAMS
# dictionary. Could be deprecated and removed.
GLOBAL_OPTIONS, GLOBAL_ARGS = None, None


# Monkey patching to make Gevent.pool compatible with
# ruffus.
OLD_NEXT = gevent.pool.IMapUnordered.next


class EventPool(gevent.pool.Pool):

    def __len__(self):
        """make sure that pool always evaluates to true."""
        l = gevent.pool.Pool.__len__(self)
        if not l:
            return 1
        return l

    def close(self):
        pass

    def terminate(self):
        self.kill()

    @staticmethod
    def next_with_timeout(instance, timeout=None):
        """ignore timeout parameter."""
        return OLD_NEXT(instance)

gevent.pool.IMapUnordered.next = EventPool.next_with_timeout


def get_logger():
    return logging.getLogger("daisy.pipeline")


def write_config_files(pipeline_path, general_path):
    '''create default configuration files in `path`.
    '''

    paths = [pipeline_path, general_path]
    config_files = ['pipeline.ini', 'conf.py']

    for dest in config_files:
        if os.path.exists(dest):
            E.warn("file `%s` already exists - skipped" % dest)
            continue

        for path in paths:
            src = os.path.join(path, dest)
            if os.path.exists(src):
                shutil.copyfile(src, dest)
                E.info("created new configuration file `%s` " % dest)
                break
        else:
            raise ValueError(
                "default config file for `%s` not found in %s" %
                (config_files, paths))


def print_config_files():
    '''
        Print the list of .ini files used to configure the pipeline
        along with their associated priorities.
        Priority 1 is the highest.
    '''

    filenames = PARAMS['pipeline_ini']
    print("\n List of .ini files used to configure the pipeline")
    s = len(filenames)
    if s == 0:
        print(" No ini files passed!")
    elif s >= 1:
        print(" %-11s: %s " % ("Priority", "File"))
        for f in filenames:
            if s == 1:
                print(" (highest) %s: %s\n" % (s, f))
            else:
                print(" %-11s: %s " % (s, f))
            s -= 1


def clone_pipeline(srcdir, destdir=None):
    '''clone a pipeline.

    Cloning entails creating a mirror of the source pipeline.
    Generally, data files are mirrored by linking. Configuration
    files and the pipeline database will be copied.

    Without modification of any files, building the cloned pipeline in
    `destdir` should not re-run any commands. However, on deleting
    selected files, the pipeline should run from the appropriate
    point.  Newly created files will not affect the original pipeline.

    Cloning pipelines permits sharing partial results between
    pipelines, for example for parameter optimization.

    Arguments
    ---------
    scrdir : string
        Source directory
    destdir : string
        Destination directory. If None, use the current directory.

    '''

    if destdir is None:
        destdir = os.path.curdir

    get_logger().info("cloning pipeline from %s to %s" % (srcdir, destdir))

    copy_files = ("conf.py", "pipeline.ini", "benchmark.yml", "csvdb")
    ignore_prefix = (
        "report", "_cache", "export", "tmp", "ctmp",
        "_static", "_templates", "shell.log", "pipeline.log",
        "results.commit")

    def _ignore(p):
        for x in ignore_prefix:
            if p.startswith(x):
                return True
        return False

    for root, dirs, files in os.walk(srcdir):

        relpath = os.path.relpath(root, srcdir)
        if _ignore(relpath):
            continue

        for d in dirs:
            if _ignore(d):
                continue
            dest = os.path.join(os.path.join(destdir, relpath, d))
            os.mkdir(dest)
            # touch
            s = os.stat(os.path.join(root, d))
            os.utime(dest, (s.st_atime, s.st_mtime))

        for f in files:
            if _ignore(f):
                continue

            fn = os.path.join(root, f)
            dest_fn = os.path.join(destdir, relpath, f)
            if f in copy_files:
                shutil.copyfile(fn, dest_fn)
            else:
                # realpath resolves links - thus links will be linked to
                # the original target
                os.symlink(os.path.realpath(fn),
                           dest_fn)


def clean(files, logfile):
    '''clean up files given by glob expressions.

    Files are cleaned up by zapping, i.e. the files are set to size
    0. Links to files are replaced with place-holders.

    Information about the original file is written to `logfile`.

    Arguments
    ---------
    files : list
        List of glob expressions of files to clean up.
    logfile : string
        Filename of logfile.

    '''
    fields = ('st_atime', 'st_blksize', 'st_blocks',
              'st_ctime', 'st_dev', 'st_gid', 'st_ino',
              'st_mode', 'st_mtime', 'st_nlink',
              'st_rdev', 'st_size', 'st_uid')

    dry_run = PARAMS.get("dryrun", False)

    if not dry_run:
        if not os.path.exists(logfile):
            outfile = IOTools.open_file(logfile, "w")
            outfile.write("filename\tzapped\tlinkdest\t%s\n" %
                          "\t".join(fields))
        else:
            outfile = IOTools.open_file(logfile, "a")

    c = E.Counter()
    for fn in files:
        c.files += 1
        if not dry_run:
            stat, linkdest = IOTools.zap_file(fn)
            if stat is not None:
                c.zapped += 1
                if linkdest is not None:
                    c.links += 1
                outfile.write("%s\t%s\t%s\t%s\n" % (
                    fn,
                    time.asctime(time.localtime(time.time())),
                    linkdest,
                    "\t".join([str(getattr(stat, x)) for x in fields])))

    get_logger().info("zapped: %s" % (c))
    outfile.close()

    return c


def peek_parameters(workingdir,
                    pipeline,
                    on_error_raise=None,
                    prefix=None,
                    update_interface=False,
                    restrict_interface=False):
    '''peek configuration parameters from external pipeline.

    As the paramater dictionary is built at runtime, this method
    executes the pipeline in workingdir, dumping its configuration
    values and reading them into a dictionary.

    If either `pipeline` or `workingdir` are not found, an error is
    raised. This behaviour can be changed by setting `on_error_raise`
    to False. In that case, an empty dictionary is returned.

    Arguments
    ---------
    workingdir : string
       Working directory. This is the directory that the pipeline
       was executed in.
    pipeline : string
       Name of the pipeline script. The pipeline is assumed to live
       in the same directory as the current pipeline.
    on_error_raise : Bool
       If set to a boolean, an error will be raised (or not) if there
       is an error during parameter peeking, for example if
       `workingdir` can not be found. If `on_error_raise` is None, it
       will be set to the default, which is to raise an exception
       unless the calling script is imported or the option
       ``--is-test`` has been passed at the command line.
    prefix : string
       Add a prefix to all parameters. This is useful if the paramaters
       are added to the configuration dictionary of the calling pipeline.
    update_interface : bool
       If True, this method will prefix any options in the
       ``[interface]`` section with `workingdir`. This allows
       transparent access to files in the external pipeline.
    restrict_interface : bool
       If  True, only interface parameters will be imported.

    Returns
    -------
    config : dict
        Dictionary of configuration values.

    '''
    caller_locals = get_caller_locals()

    # check if we should raise errors
    if on_error_raise is None:
        on_error_raise = not isTest() and \
            "__name__" in caller_locals and \
            caller_locals["__name__"] == "__main__"

    # patch - if --help or -h in command line arguments,
    # do not peek as there might be no config file.
    if "--help" in sys.argv or "-h" in sys.argv:
        return {}

    # Attempt to locate directory with pipeline source code. This is a
    # patch as pipelines might be called within the repository
    # directory or from an installed location
    dirname = PARAMS["pipelinedir"]

    # called without a directory, use current directory
    if dirname == "":
        dirname = os.path.abspath(".")
    else:
        # if not exists, assume we want version located
        # in directory of calling script.
        if not os.path.exists(dirname):
            # directory is path of calling script
            dirname = os.path.dirname(caller_locals['__file__'])

    pipeline = os.path.join(dirname, pipeline)
    if not os.path.exists(pipeline):
        if on_error_raise:
            raise ValueError(
                "can't find pipeline at %s" % (pipeline))
        else:
            return {}

    if workingdir == "":
        workingdir = os.path.abspath(".")

    # patch for the "config" target - use default
    # pipeline directory if directory is not specified
    # working dir is set to "?!"
    if ("config" in sys.argv or "check" in sys.argv or "clone" in sys.argv
        and workingdir == "?!"):
        workingdir = os.path.join(PARAMS.get("pipelinedir"),
                                  IOTools.snip(pipeline, ".py"))

    if not os.path.exists(workingdir):
        if on_error_raise:
            raise ValueError(
                "can't find working dir %s" % workingdir)
        else:
            return {}

    statement = "python %s -f -v 0 dump" % pipeline
    process = subprocess.Popen(statement,
                               cwd=workingdir,
                               shell=True,
                               stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE)

    # process.stdin.close()
    stdout, stderr = process.communicate()
    if process.returncode != 0:
        raise OSError(
            ("Child was terminated by signal %i: \n"
             "Statement: %s\n"
             "The stderr was: \n%s\n"
             "Stdout: %s") %
            (-process.returncode, statement, stderr, stdout))

    # subprocess only accepts encoding argument in py >= 3.6 so
    # decode here.
    stdout = stdout.decode("utf-8").splitlines()
    # remove any log messages
    stdout = [x for x in stdout if x.startswith("{")]
    if len(stdout) > 1:
        raise ValueError("received multiple configurations")
    dump = json.loads(stdout[0])

    # update interface
    if update_interface:
        for key, value in list(dump.items()):
            if key.startswith("interface"):
                dump[key] = os.path.join(workingdir, value)

    # keep only interface if so required
    if restrict_interface:
        dump = dict([(k, v) for k, v in dump.items()
                     if k.startswith("interface")])

    # prefix all parameters
    if prefix is not None:
        dump = dict([("%s%s" % (prefix, x), y) for x, y in list(dump.items())])

    return dump


class LoggingFilterPipelineName(logging.Filter):
    """add pipeline name to log message.

    With this filter, %(app_name)s can be used in log formats.
    """

    def __init__(self, name, *args, **kwargs):
        logging.Filter.__init__(self, *args, **kwargs)
        self.app_name = name

    def filter(self, record):
        record.app_name = self.app_name
        message = record.getMessage()
        if message.startswith("- {"):
            json_message = json.loads(message[2:])
        elif message.startswith("{"):
            json_message = json.loads(message)
        else:
            json_message = None
        if json_message:
            for k, v in list(json_message.items()):
                setattr(record, k, v)

        return True


def ruffus_return_dag(stream,
                      target_tasks=[],
                      forcedtorun_tasks=[],
                      verbose=None,
                      gnu_make_maximal_rebuild_mode=True,
                      wrap_width=100,
                      runtime_data=None,
                      checksum_level=None,
                      history_file=None,
                      verbose_abbreviated_path=None,
                      pipeline=None):

    (checksum_level,
     job_history,
     pipeline,
     runtime_data,
     target_tasks,
     forcedtorun_tasks) = _pipeline_prepare_to_run(
         checksum_level,
         history_file,
         pipeline,
         runtime_data,
         target_tasks,
         forcedtorun_tasks)

    (incomplete_tasks,
     self_terminated_nodes,
     dag_violating_edges,
     dag_violating_nodes) = \
        topologically_sorted_nodes(
            target_tasks, forcedtorun_tasks,
            gnu_make_maximal_rebuild_mode,
            extra_data_for_signal=[
                t_verbose_logger(0, 0, None, runtime_data), job_history],
            signal_callback=is_node_up_to_date)

    # ignore circular dependencies
    stream.write("function\tactive\toutput_files\tparents\n")
    stack = target_tasks + forcedtorun_tasks
    visited = set()
    while stack:
        t = stack.pop(0)
        visited.add(t)
        stream.write("\t".join(
            map(str, [t.func_name,
                      t.is_active,
                      ",".join(t.output_filenames) if t.output_filenames else "",
                      ",".join(x.func_name for x in t._get_inward())])) + "\n")
        for tt in t._get_inward():
            if tt not in visited:
                stack.append(tt)


def setup_logging(options, pipeline=None):

    logger = logging.getLogger("daisy.pipeline")

    if options.log_config_filename is None:

        # set up default file logger
        handler = logging.FileHandler(
            filename=options.pipeline_logfile,
            mode="a")

        if pipeline is not None:
            pipeline_name = pipeline.name
        else:
            pipeline_name = "main"

        handler.setFormatter(
            E.MultiLineFormatter(
                "%(asctime)s %(levelname)s "
                "%(app_name)s %(module)s "
                "- %(message)s"))

        logger.addFilter(LoggingFilterPipelineName(name=pipeline_name))
        logger.addHandler(handler)

        logger.info("pipeline log is {}".format(
            options.pipeline_logfile))

    return logger


def get_version():
    # try git:
    try:
        stdout, stderr = execute(
            "git rev-parse HEAD", cwd=PARAMS["scriptsdir"])
    except:
        stdout = "NA"
    return stdout

USAGE = '''
usage: %prog [OPTIONS] [CMD] [target]

Execute pipeline %prog.

Commands can be any of the following

make <target>
   run all tasks required to build *target*

show <target>
   show tasks required to build *target* without executing them

plot <target>
   plot image (using inkscape) of pipeline state for *target*

debug <target> [args]
   debug a method using the supplied arguments. The method <target>
   in the pipeline is run without checking any dependencies.

config
   write new configuration files pipeline.ini, sphinxreport.ini and conf.py
   with default values

dump
   write pipeline configuration to stdout

touch
   touch files only, do not run

regenerate
   regenerate the ruffus checkpoint file

check
   check if requirements (external tool dependencies) are satisfied.

clone <source>
   create a clone of a pipeline in <source> in the current
   directory. The cloning process aims to use soft linking to files
   (not directories) as much as possible.  Time stamps are
   preserved. Cloning is useful if a pipeline needs to be re-run from
   a certain point but the original pipeline should be preserved.

'''


def parse_commandline(argv=None, **kwargs):
    """parse command line.

    Create option parser and parse command line.

    Arguments
    ---------
    argv : list
        List of command line options to parse. If None, use sys.argv.

    **kwargs: dict
        Additional arguments overwrite default option settings.

    Returns
    -------

    options: object
       Command line options container

    args : list
       List of command line arguments

    """
    if argv is None:
        argv = sys.argv

    parser = E.OptionParser(version="%prog version: $Id$",
                            usage=USAGE)

    parser.add_option("--pipeline-action", dest="pipeline_action",
                      type="choice",
                      choices=(
                          "make", "show", "plot", "dump", "config", "clone",
                          "check", "regenerate", "state", "printconfig"),
                      help="action to take [default=%default].")

    parser.add_option("--pipeline-format", dest="pipeline_format",
                      type="choice",
                      choices=("dot", "jpg", "svg", "ps", "png"),
                      help="pipeline format [default=%default].")

    parser.add_option("-n", "--dry-run", dest="dry_run",
                      action="store_true",
                      help="perform a dry run (do not execute any shell "
                      "commands) [default=%default].")

    parser.add_option("-c", "--config-file", dest="config_file",
                      help="benchmark configuration file "
                      "[default=%default].")

    parser.add_option("-f", "--force-run", dest="force_run",
                      type="string",
                      help="force running the pipeline even if there are "
                      "up-to-date tasks. If option is 'all', all tasks "
                      "will be rerun. Otherwise, only the tasks given as "
                      "arguments will be rerun. "
                      "[default=%default].")

    parser.add_option("-p", "--multiprocess", dest="multiprocess", type="int",
                      help="number of parallel processes to use on "
                      "submit host "
                      "(different from number of jobs to use for "
                      "cluster jobs) "
                      "[default=%default].")

    parser.add_option("-e", "--exceptions", dest="log_exceptions",
                      action="store_true",
                      help="echo exceptions immediately as they occur "
                      "[default=%default].")

    parser.add_option("-i", "--terminate", dest="terminate",
                      action="store_true",
                      help="terminate immediately at the first exception "
                      "[default=%default].")

    parser.add_option("-d", "--debug", dest="debug",
                      action="store_true",
                      help="output debugging information on console, "
                      "and not the logfile "
                      "[default=%default].")

    parser.add_option("-s", "--set", dest="variables_to_set",
                      type="string", action="append",
                      help="explicitely set paramater values "
                      "[default=%default].")

    parser.add_option("--checksums", dest="ruffus_checksums_level",
                      type="int",
                      help="set the level of ruffus checksums"
                      "[default=%default].")

    parser.add_option("-t", "--is-test", dest="is_test",
                      action="store_true",
                      help="this is a test run"
                      "[default=%default].")

    parser.add_option("--engine", dest="engine",
                      choices=("local", "arvados"),
                      help="engine to use."
                      "[default=%default].")

    parser.add_option(
        "--always-mount", dest="always_mount",
        action="store_true",
        help="force mounting of arvados keep [%default]")

    parser.add_option("--only-info", dest="only_info",
                      action="store_true",
                      help="only update meta information, do not run "
                      "[default=%default].")

    parser.add_option("--work-dir", dest="work_dir",
                      type="string",
                      help="working directory. Will be created if it does not exist "
                      "[default=%default].")

    group = E.OptionGroup(parser, "Pipeline logging configuration")

    group.add_option("--pipeline-logfile", dest="pipeline_logfile",
                     type="string",
                     help="primary logging destination."
                     "[default=%default].")

    group.add_option("--shell-logfile", dest="shell_logfile",
                     type="string",
                     help="filename for shell debugging information. "
                     "If it is not an absolute path, "
                     "the output will be written into the current working "
                     "directory. If unset, no logging will be output. "
                     "[default=%default].")

    parser.add_option("--input-validation", dest="input_validation",
                      action="store_true",
                      help="perform input validation before starting "
                      "[default=%default].")

    parser.add_option_group(group)

    parser.set_defaults(
        pipeline_action=None,
        pipeline_format="svg",
        pipeline_targets=[],
        force_run=False,
        multiprocess=None,
        pipeline_logfile="pipeline.log",
        shell_logfile=None,
        dry_run=False,
        log_exceptions=True,
        engine="local",
        exceptions_terminate_immediately=None,
        debug=False,
        variables_to_set=[],
        is_test=False,
        ruffus_checksums_level=0,
        config_file="benchmark.yml",
        work_dir=None,
        always_mount=False,
        only_info=False,
        input_validation=False)

    parser.set_defaults(**kwargs)

    if "callback" in kwargs:
        kwargs["callback"](parser)

    logger_callback = setup_logging

    (options, args) = E.start(
        parser,
        add_cluster_options=True,
        argv=argv,
        logger_callback=logger_callback)

    return options, args


def update_params_with_commandline_options(params, options):
    """add and update selected parameters in the parameter
    dictionary with command line options.
    """

    params["dryrun"] = options.dry_run
    if options.cluster_queue is not None:
        params["cluster_queue"] = options.cluster_queue
    if options.cluster_priority is not None:
        params["cluster_priority"] = options.cluster_priority
    if options.cluster_num_jobs is not None:
        params["cluster_num_jobs"] = options.cluster_num_jobs
    if options.cluster_options is not None:
        params["cluster_options"] = options.cluster_options
    if options.cluster_parallel_environment is not None:
        params["cluster_parallel_environment"] =\
            options.cluster_parallel_environment
    if options.without_cluster:
        params["without_cluster"] = True

    params["shell_logfile"] = options.shell_logfile

    params["ruffus_checksums_level"] = options.ruffus_checksums_level

    for variables in options.variables_to_set:
        variable, value = variables.split("=")
        value = IOTools.str2val(value.strip())
        # enter old style
        params[variable.strip()] = value
        # enter new style
        parts = variable.split("_")
        for x in range(1, len(parts)):
            prefix = "_".join(parts[:x])
            if prefix in params:
                suffix = "_".join(parts[x:])
                params[prefix][suffix] = value


@contextlib.contextmanager
def cache_os_functions():
    os.stat = cached_os_stat
    os.path.abspath = cached_os_path_abspath
    os.path.realpath = cached_os_path_realpath
    os.path.relpath = cached_os_path_relpath
    os.path.islink = cached_os_path_islink

    yield

    os.stat = SAVED_OS_STAT
    os.path.abspath = SAVED_OS_PATH_ABSPATH
    os.path.realpath = SAVED_OS_PATH_REALPATH
    os.path.relpath = SAVED_OS_PATH_RELPATH
    os.path.islink = SAVED_OS_PATH_ISLINK


class LoggingFilterProgress(logging.Filter):
    """add progress information to the log-stream.

    A :term:`task` is a ruffus_ decorated function, which will execute
    one or more :term:`jobs`.

    Valid task/job status:
    update
       task/job needs updating
    completed
       task/job completed successfully
    failed
       task/job failed
    running
       task/job is running
    ignore
       ignore task/job (is up-to-date)

    This filter adds the following context to a log record:

    task
       task_name

    task_status
       task status

    task_total
       number of jobs in task

    task_completed
       number of jobs in task completed

    task_completed_percent
       percentage of task completed

    The filter will also generate an additional log message in json format
    with the fields above.

    Arguments
    ---------
    ruffus_text : string
        Log messages from ruffus.pipeline_printout. These are used
        to collect all tasks that will be executed during pipeline
        execution.

    """
    def __init__(self,
                 ruffus_text):

        # dictionary of jobs to run
        self.jobs = {}
        self.tasks = {}
        self.map_job2task = {}
        self.logger = get_logger()

        def split_by_job(text):
            # ignore optional docstring at beginning (is bracketed by '"')
            text = re.sub('^\"[^"]+\"', "", "".join(text))
            for line in re.split("Job\s+=", text):
                if not line.strip():
                    continue
                if "Make missing directories" in line:
                    continue
                try:
                    # long file names cause additional wrapping and
                    # additional white-space characters
                    job_name = re.search(
                        r"\[.*-> ([^\]]+)\]", line).groups()[0]
                except AttributeError:
                    continue
                    # raise AttributeError("could not parse '%s'" % line)
                job_status = "ignore"
                if "Job needs update" in line:
                    job_status = "update"

                yield job_name, job_status

        def split_by_task(text):
            block, task_name = [], None
            task_status = None
            for line in text.splitlines():
                line = line.strip()

                if line.startswith("Tasks which will be run"):
                    task_status = "update"
                    block = []
                    continue
                elif line.startswith("Tasks which are up-to-date"):
                    task_status = "ignore"
                    block = []
                    continue

                if line.startswith("Task = "):
                    if task_name:
                        yield task_name, task_status, list(split_by_job(block))
                    block = []
                    task_name = re.match("Task = (.*)", line).groups()[0]
                    continue
                if line:
                    block.append(line)
            if task_name:
                yield task_name, task_status, list(split_by_job(block))

        # populate with initial messages
        for task_name, task_status, jobs in split_by_task(ruffus_text):
            if task_name.startswith("(mkdir"):
                continue

            to_run = 0
            for job_name, job_status in jobs:
                self.jobs[job_name] = (task_name, job_name)
                if job_status == "update":
                    to_run += 1
                self.map_job2task[re.sub("\s", "", job_name)] = task_name

            self.tasks[task_name] = [task_status,
                                     len(jobs),
                                     len(jobs) - to_run]

    def filter(self, record):

        if not record.filename.endswith("task.py"):
            return True

        # update task counts and status
        job_name, task_name = None, None
        if re.search(r"Job\s+=", record.msg):
            try:
                job_name = re.search(
                    r"\[.*-> ([^\]]+)\]", record.msg).groups()[0]
            except AttributeError:
                return True
            job_name = re.sub("\s", "", job_name)
            task_name = self.map_job2task.get(job_name, None)
            if task_name is None:
                return
            if "completed" in record.msg:
                self.tasks[task_name][2] += 1

        elif re.search(r"Task\s+=", record.msg):
            try:
                before, task_name = record.msg.strip().split(" = ")
            except ValueError:
                return True

            # ignore the mkdir, etc tasks
            if task_name not in self.tasks:
                return True

            if before == "Task enters queue":
                self.tasks[task_name][0] = "running"
            elif before == "Completed Task":
                self.tasks[task_name][0] = "completed"
            elif before == "Uptodate Task":
                self.tasks[task_name][0] = "uptodate"
            else:
                return True
        else:
            return True

        if task_name is None:
            return

        # update log record
        task_status, task_total, task_completed = self.tasks[task_name]
        if task_total > 0:
            task_completed_percent = 100.0 * task_completed / task_total
        else:
            task_completed_percent = 0

        # ignore prefix:: in task_name for output
        task_name = re.sub("^[^:]+::", "", task_name)
        data = {
            "task": task_name,
            "task_status": task_status,
            "task_total": task_total,
            "task_completed": task_completed,
            "task_completed_percent": task_completed_percent}

        record.task_status = task_status
        record.task_total = task_total
        record.task_completed = task_completed
        record.task_completed_percent = task_completed_percent

        # log status
        self.logger.info(json.dumps(data))

        return True


def main(options, args, pipeline=None):
    """command line control function for a pipeline.

    This method defines command line options for the pipeline and
    updates the global configuration dictionary correspondingly.

    It then provides a command parser to execute particular tasks
    using the ruffus pipeline control functions. See the generated
    command line help for usage.

    To use it, add::

        import Pipeline as P

        if __name__ == "__main__":
            sys.exit(P.main(sys.argv))

    to your pipeline script.

    Arguments
    ---------
    options: object
        Container for command line arguments.
    args : list
        List of command line arguments.
    pipeline: object
        Pipeline to run. If not given, all ruffus pipelines are run.

    """

    global GLOBAL_OPTIONS
    global GLOBAL_ARGS

    GLOBAL_OPTIONS, GLOBAL_ARGS = options, args
    logger = logging.getLogger("daisy.pipeline")

    logger.info("started in workingdir: {}".format(PARAMS.get("workingdir")))
    # At this point, the PARAMS dictionary has already been
    # built. It now needs to be updated with selected command
    # line options as these should always take precedence over
    # configuration files.
    update_params_with_commandline_options(PARAMS, options)

    version = get_version()

    if args:
        options.pipeline_action = args[0]
        if len(args) > 1:
            options.pipeline_targets.extend(args[1:])

    if options.force_run:
        if options.force_run == "all":
            forcedtorun_tasks = pipeline_get_task_names()
        else:
            forcedtorun_tasks = options.pipeline_targets
    else:
        forcedtorun_tasks = []

    # create local scratch if it does not already exists. Note that
    # directory itself will be not deleted while its contents should
    # be cleaned up.
    if not os.path.exists(PARAMS["tmpdir"]):
        logger.warn("local temporary directory {} did not exist - created".format(
            PARAMS["tmpdir"]))
        try:
            os.makedirs(PARAMS["tmpdir"])
        except OSError:
            # file exists
            pass

    logger.debug("temporary directory is {}".format(PARAMS["tmpdir"]))

    # set multiprocess to a sensible setting if there is no cluster
    run_on_cluster = HAS_DRMAA is True and not options.without_cluster
    if options.multiprocess is None:
        if not run_on_cluster:
            options.multiprocess = int(math.ceil(
                multiprocessing.cpu_count() / 2.0))
        else:
            options.multiprocess = 40

    # see inputValidation function in Parameters.py
    if options.input_validation:
        input_validation(PARAMS, sys.argv[0])

    if options.pipeline_action == "check":
        counter, requirements = Requirements.checkRequirementsFromAllModules()
        for requirement in requirements:
            logger.info("\t".join(map(str, requirement)))
        logger.info("version check summary: %s" % str(counter))
        E.stop()
        return

    elif options.pipeline_action == "debug":
        # create the session proxy
        start_session()

        method_name = options.pipeline_targets[0]
        caller = get_caller()
        method = getattr(caller, method_name)
        method(*options.pipeline_targets[1:])

    elif options.pipeline_action in ("make",
                                     "show",
                                     "state",
                                     "svg",
                                     "plot",
                                     "dot",
                                     "touch",
                                     "regenerate"):

        messenger = None
        try:
            with cache_os_functions():
                if options.pipeline_action == "make":

                    # get tasks to be done. This essentially replicates
                    # the state information within ruffus.
                    stream = StringIO()
                    ruffus.pipeline_printout(
                        stream,
                        options.pipeline_targets,
                        verbose=5,
                        pipeline=pipeline,
                        checksum_level=options.ruffus_checksums_level)

                    messenger = LoggingFilterProgress(stream.getvalue())
                    logger.addFilter(messenger)

                    global task
                    if options.without_cluster:
                        # use ThreadPool to avoid taking multiple CPU for pipeline
                        # controller.
                        ruffus.task.Pool = ThreadPool
                    else:
                        # use cooperative multitasking instead of multiprocessing.
                        ruffus.task.Pool = EventPool
                        ruffus.task.queue = gevent.queue

                        # create the session proxy
                        start_session()

                    logger.info("code location: {}".format(PARAMS["scriptsdir"]))
                    logger.info("code version: {}".format(version))
                    logger.info("working directory is: {}".format(PARAMS["workingdir"]))
                    ruffus.pipeline_run(
                        options.pipeline_targets,
                        forcedtorun_tasks=forcedtorun_tasks,
                        multiprocess=options.multiprocess,
                        logger=logger,
                        verbose=options.loglevel,
                        log_exceptions=options.log_exceptions,
                        exceptions_terminate_immediately=options.exceptions_terminate_immediately,
                        checksum_level=options.ruffus_checksums_level,
                        pipeline=pipeline,
                        one_second_per_job=False,
                    )

                    close_session()

                elif options.pipeline_action == "show":
                    ruffus.pipeline_printout(
                        options.stdout,
                        options.pipeline_targets,
                        forcedtorun_tasks=forcedtorun_tasks,
                        verbose=options.loglevel,
                        pipeline=pipeline,
                        checksum_level=options.ruffus_checksums_level)

                elif options.pipeline_action == "touch":
                    ruffus.pipeline_run(
                        options.pipeline_targets,
                        touch_files_only=True,
                        verbose=options.loglevel,
                        pipeline=pipeline,
                        checksum_level=options.ruffus_checksums_level)

                elif options.pipeline_action == "regenerate":
                    ruffus.pipeline_run(
                        options.pipeline_targets,
                        touch_files_only=options.ruffus_checksums_level,
                        pipeline=pipeline,
                        verbose=options.loglevel)

                elif options.pipeline_action == "svg":
                    ruffus.pipeline_printout_graph(
                        options.stdout.buffer,
                        options.pipeline_format,
                        options.pipeline_targets,
                        forcedtorun_tasks=forcedtorun_tasks,
                        pipeline=pipeline,
                        checksum_level=options.ruffus_checksums_level)

                elif options.pipeline_action == "state":
                    ruffus.ruffus_return_dag(
                        options.stdout,
                        target_tasks=options.pipeline_targets,
                        forcedtorun_tasks=forcedtorun_tasks,
                        verbose=options.loglevel,
                        pipeline=pipeline,
                        checksum_level=options.ruffus_checksums_level)

                elif options.pipeline_action == "plot":
                    outf, filename = tempfile.mkstemp()
                    ruffus.pipeline_printout_graph(
                        os.fdopen(outf, "wb"),
                        options.pipeline_format,
                        options.pipeline_targets,
                        pipeline=pipeline,
                        checksum_level=options.ruffus_checksums_level)
                    execute("inkscape %s" % filename)
                    os.unlink(filename)

        except ruffus.ruffus_exceptions.RethrownJobError as ex:

            if not options.debug:
                E.error("%i tasks with errors, please see summary below:" %
                        len(ex.args))
                for idx, e in enumerate(ex.args):
                    task, job, error, msg, traceback = e

                    if task is None:
                        # this seems to be errors originating within ruffus
                        # such as a missing dependency
                        # msg then contains a RethrownJobJerror
                        msg = str(msg)
                    else:
                        task = re.sub("__main__.", "", task)
                        job = re.sub(r"\s", "", job)

                    # display only single line messages
                    if len([x for x in msg.split("\n") if x != ""]) > 1:
                        msg = ""

                    E.error("%i: Task=%s Error=%s %s: %s" %
                            (idx, task, error, job, msg))

                E.error("full traceback is in %s" % options.pipeline_logfile)

                logger.error("start of all error messages")
                logger.error(ex)
                logger.error("end of all error messages")

                raise ValueError("pipeline failed with %i errors" % len(ex.args)) from ex
            else:
                raise

    elif options.pipeline_action == "dump":
        options.stdout.write((json.dumps(PARAMS)) + "\n")

    elif options.pipeline_action == "printconfig":
        E.info("printing out pipeline parameters: ")
        for k in sorted(PARAMS):
            print(k, "=", PARAMS[k])
        print_config_files()

    elif options.pipeline_action == "config":
        f = sys._getframe(1)
        caller = f.f_globals["__file__"]
        pipeline_path = os.path.splitext(caller)[0]
        general_path = os.path.join(os.path.dirname(pipeline_path),
                                    "configuration")
        write_config_files(pipeline_path, general_path)

    elif options.pipeline_action == "clone":
        clone_pipeline(options.pipeline_targets[0])

    else:
        raise ValueError("unknown pipeline action %s" %
                         options.pipeline_action)

    E.stop(logger=get_logger())
