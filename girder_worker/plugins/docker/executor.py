import os
import re
import girder_worker.utils
import subprocess

from girder_worker import config, TaskSpecValidationError


def _pull_image(image):
    """
    Pulls the specified docker image onto this worker.
    """
    command = ('docker', 'pull', image)
    p = subprocess.Popen(args=command, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE)
    stdout, stderr = p.communicate()

    if p.returncode != 0:
        print('Error pulling docker image %s:' % image)
        print('STDOUT: ' + stdout)
        print('STDERR: ' + stderr)

        raise Exception('Docker pull returned code {}.'.format(p.returncode))


def _read_from_config(key, default):
    """
    Helper to read docker specific config values from the worker config files.
    """
    if config.has_option('docker', key):
        return config.get('docker', key)
    else:
        return default


def _transform_path(inputs, taskInputs, inputId, tmpDir):
    """
    If the input specified by inputId is a filepath target, we transform it to
    its absolute path within the docker container (underneath /data).
    """
    for ti in taskInputs.itervalues():
        tiId = ti['id'] if 'id' in ti else ti['name']
        if tiId == inputId:
            if ti.get('target') == 'filepath':
                rel = os.path.relpath(inputs[inputId]['script_data'], tmpDir)
                return os.path.join('/data', rel)
            else:
                return inputs[inputId]['script_data']

    raise Exception('No task input found with id = ' + inputId)


def _expand_args(args, inputs, taskInputs, tmpDir):
    """
    Expands arguments to the container execution if they reference input
    data. For example, if an input has id=foo, then a container arg of the form
    $input{foo} would be expanded to the runtime value of that input. If that
    input is a filepath target, the file path will be transformed into the
    location that it will be available inside the running container.
    """
    newArgs = []
    regex = re.compile(r'\$input\{([^}]+)\}')

    for arg in args:
        for inputId in re.findall(regex, arg):
            if inputId in inputs:
                transformed = _transform_path(inputs, taskInputs, inputId,
                                              tmpDir)
                arg = arg.replace('$input{%s}' % inputId, transformed)
            elif inputId == '_tempdir':
                arg = arg.replace('$input{_tempdir}', '/data')

        newArgs.append(arg)

    return newArgs


def _docker_gc(tempdir):
    """
    Garbage collect containers that have not been run in the last hour using the
    https://github.com/spotify/docker-gc project's script, which is copied in
    the same directory as this file. After that, deletes all images that are
    no longer used by any containers.

    This starts the script in the background and returns the subprocess object.
    Waiting for the subprocess to complete is left to the caller, in case they
    wish to do something in parallel with the garbage collection.

    Standard output and standard error pipes from this subprocess are the same
    as the current process to avoid blocking on a full buffer.

    :param tempdir: Temporary directory where the GC should write files.
    :type tempdir: str
    :returns: The process object that was created.
    :rtype: `subprocess.Popen`
    """
    script = os.path.join(os.path.dirname(__file__), 'docker-gc')
    if not os.path.isfile(script):
        raise Exception('Docker GC script %s not found.' % script)
    if not os.access(script, os.X_OK):
        raise Exception('Docker GC script %s is not executable.' % script)

    env = os.environ.copy()
    env['FORCE_CONTAINER_REMOVAL'] = '1'
    env['STATE_DIR'] = tempdir
    env['PID_DIR'] = tempdir
    env['GRACE_PERIOD_SECONDS'] = str(_read_from_config('cache_timeout', 3600))

    # Handle excluded images
    excluded = _read_from_config('exclude_images', '').split(',')
    excluded = [img for img in excluded if img.strip()]
    if excluded:
        exclude_file = os.path.join(tempdir, '.docker-gc-exclude')
        with open(exclude_file, 'w') as fd:
            fd.write('\n'.join(excluded) + '\n')
        env['EXCLUDE_FROM_GC'] = exclude_file

    return subprocess.Popen(args=(script,), env=env)


def validate_task_outputs(task_outputs):
    """
    This is called prior to fetching inputs to make sure the output specs are
    valid. Outputs in docker mode can result in side effects, so it's best to
    make sure the specs are valid prior to fetching.
    """
    for name, spec in task_outputs.iteritems():
        if spec.get('target') == 'filepath':
            path = spec.get('path', name)
            if path.startswith('/') and not path.startswith('/data/'):
                raise TaskSpecValidationError(
                    'Docker filepath output paths must either start with '
                    '"/data/" or be specified relative to the /data dir.')
        elif name not in ('_stdout', '_stderr'):
            raise TaskSpecValidationError(
                'Docker outputs must be either "_stdout", "_stderr", or '
                'filepath-target outputs.')


def run(task, inputs, outputs, task_inputs, task_outputs, **kwargs):
    image = task['docker_image']

    if task.get('pull_image', True):
        print('Pulling docker image: ' + image)
        _pull_image(image)

    tempdir = kwargs.get('_tempdir')
    args = _expand_args(task.get('container_args', []), inputs, task_inputs,
                        tempdir)

    print_stderr, print_stdout = True, True
    for id, to in task_outputs.iteritems():
        if id == '_stderr':
            outputs['_stderr']['script_data'] = ''
            print_stderr = False
        elif id == '_stdout':
            outputs['_stdout']['script_data'] = ''
            print_stdout = False

    command = ['docker', 'run', '-u', str(os.getuid())]

    if tempdir:
        command += ['-v', tempdir + ':/data']

    if 'entrypoint' in task:
        command += ['--entrypoint', task['entrypoint']]

    if 'docker_run_args' in task:
        command += task['docker_run_args']

    command += [image] + args

    print('Running container: "%s"' % ' '.join(command))

    p = girder_worker.utils.run_process(command, outputs,
                                        print_stdout, print_stderr)

    if p.returncode != 0:
        raise Exception('Error: docker run returned code %d.' % p.returncode)

    print('Garbage collecting old containers and images.')
    gc_dir = os.path.join(tempdir, 'docker_gc_scratch')
    os.mkdir(gc_dir)
    p = _docker_gc(gc_dir)

    for name, task_output in task_outputs.iteritems():
        if task_output.get('target') == 'filepath':
            path = task_output.get('path', name)
            if not path.startswith('/'):
                # Assume relative paths are relative to /data
                path = '/data/' + path

            # Convert "/data/" to the temp dir
            path = path.replace('/data', tempdir, 1)
            if not os.path.exists(path):
                raise Exception('Output filepath %s does not exist.' % path)
            outputs[name]['script_data'] = path

    p.wait()  # Wait for garbage collection subprocess to finish

    if p.returncode != 0:
        raise Exception('Docker GC returned code %d.' % p.returncode)
