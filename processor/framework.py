import os
import re
import luigi
import law
from law.util import interruptable_popen
from subprocess import PIPE
from rich.console import Console
from law.util import merge_dicts
import socket
from datetime import datetime
import yaml
from XRootD import client
from re import findall

law.contrib.load("wlcg")
law.contrib.load("htcondor")
console = Console()


class Task(law.Task):
    wlcg_path = luigi.Parameter()
    production_tag = luigi.Parameter(default="default")

    def store_parts(self):
        return (self.__class__.__name__,)

    def local_path(self, *path):
        parts = (os.getenv("ANALYSIS_DATA_PATH"),) + (self.__class__.__name__,) + path
        return os.path.join(*parts)

    def local_target(self, *path):
        return law.LocalFileTarget(self.local_path(*path))

    def convert_env_to_dict(self, env):
        my_env = {}
        for line in env.splitlines():
            if line.find(" ") < 0:
                try:
                    key, value = line.split("=", 1)
                    my_env[key] = value
                except ValueError:
                    pass
        return my_env

    def set_environment(self, sourcescript):
        code, out, error = interruptable_popen(
            "source {}; env".format(sourcescript),
            shell=True,
            stdout=PIPE,
            stderr=PIPE,
        )
        if code != 0:
            console.log("Error when running source {}".format(error))
            console.log("Output: {}".format(out))
            console.log("source returned non-zero exit status {}".format(code))
            console.log("Script: {}".format(sourcescript))
            console.rule()
            raise Exception("source failed")
        my_env = self.convert_env_to_dict(out)
        return my_env

    def remote_path(self, *path):
        parts = (self.__class__.__name__,) + path
        return os.path.join(*parts)

    def remote_target(self, *path):
        target = law.wlcg.WLCGFileTarget(path=self.remote_path(*path))
        return target

    def remote_targets(self, paths):
        targets = []
        for path in paths:
            targets.append(law.wlcg.WLCGFileTarget(path=self.remote_path(path)))
        return targets


class HTCondorJobManager(law.htcondor.HTCondorJobManager):

    status_line_cre = re.compile(
        "^(\d+\.\d+)" + 4 * "\s+[^\s]+" + "\s+([UIRXSCHE<>])\s+.*$"
    )

    # def get_htcondor_version(cls):
    # return (9, 6, 0)
    #     return (8, 6, 5)


class HTCondorWorkflow(law.htcondor.HTCondorWorkflow):

    ENV_NAME = luigi.Parameter()
    production_tag = luigi.Parameter(default="default")
    htcondor_accounting_group = luigi.Parameter()
    htcondor_requirements = luigi.Parameter()
    htcondor_remote_job = luigi.Parameter()
    htcondor_user_proxy = luigi.Parameter()
    htcondor_walltime = luigi.Parameter()
    htcondor_request_cpus = luigi.Parameter()
    htcondor_request_gpus = luigi.Parameter(default="0")
    htcondor_request_memory = luigi.Parameter()
    htcondor_universe = luigi.Parameter()
    htcondor_docker_image = luigi.Parameter()
    htcondor_request_disk = luigi.Parameter()
    wlcg_path = luigi.Parameter()
    bootstrap_file = luigi.Parameter()
    replace_processor_tar = luigi.BoolParameter(default=False)

    # Use proxy file located in $X509_USER_PROXY or /tmp/x509up_u$(id) if empty
    htcondor_user_proxy = law.wlcg.get_voms_proxy_file()

    # output_collection_cls = law.SiblingFileCollection

    def htcondor_create_job_manager(self, **kwargs):
        kwargs = merge_dicts(self.htcondor_job_manager_defaults, kwargs)
        return HTCondorJobManager(**kwargs)

    def htcondor_output_directory(self):
        # Expand path to account for use of env variables (like $USER)
        return law.wlcg.WLCGDirectoryTarget(
            self.remote_path(),
            law.wlcg.WLCGFileSystem(
                None, base="{}".format(os.path.expandvars(self.wlcg_path))
            ),
        )

    def htcondor_create_job_file_factory(self):
        factory = super(HTCondorWorkflow, self).htcondor_create_job_file_factory()
        factory.is_tmp = False
        console.log("HTCondor job directory is: {}".format(factory.dir))
        return factory

    def htcondor_bootstrap_file(self):
        hostfile = self.bootstrap_file
        return law.util.rel_path(__file__, hostfile)
        
    # Split full path into url and path
    def split_xrdfs_paths(self, paths):
        full_path = "/".join(paths)
        # Split string at second '//'
        regex = '([^\/]+\/\/[^\/]+\/)(.*)'
        pieces = findall(regex, full_path)
        if len(pieces)!=1 or len(pieces[0])!=2:
            raise Exception("xrdfs_ls of {full_path} failed, given path not valid".format(
                    full_path=full_path
                )
            )
        return pieces[0]

    # Call ls via xrootd
    def xrdfs_ls(self, paths):
        url, path = self.split_xrdfs_paths(paths)
        myclient = client.FileSystem(url)
        out_data, ls_data = myclient.dirlist(path)
        if out_data.code:
            raise Exception("xrdfs_ls of {full_path} failed with:\n{message}".format(
                    full_path=full_path, message=out_data.message
                )
            )
        files = [idir.name for idir in ls_data.dirlist]
        return files

    # Remove file or dir recursive via xrootd
    def xrdfs_rm(self, paths):
        console.log("Removing " + "/".join(paths))
        url, path = self.split_xrdfs_paths(paths)
        myclient = client.FileSystem(url)
        def recursive_xrdfs_rm(new_path):
            # Try to remove dir
            out_data, res = myclient.rmdir(new_path)
            if not out_data.ok:
                # If file
                if out_data.errno == 3015:
                    out_data, res = myclient.rm(new_path)
                # If dir not empty
                elif out_data.errno == 3012:
                    out_data, ls_data = myclient.dirlist(new_path)
                    for directory in ls_data.dirlist:
                        recursive_xrdfs_rm("/".join([new_path, directory.name]))
                    out_data, res = myclient.rmdir(new_path)
                else:
                    raise Exception("xrdfs_rm of {full_path} failed with:\n{message}".format(
                            full_path=new_path, message=out_data.message
                        )
                    )
            console.log(url + new_path + " removed.")
        recursive_xrdfs_rm(path)

    def htcondor_job_config(self, config, job_num, branches):
        # Time seems somewhat off for some reason
        start_time = datetime.now().strftime("%Y_%m_%d_%H_%m_%S_%f")
        analysis_name = os.getenv("ANA_NAME")
        task_name = self.__class__.__name__
        # Check if env in config file is still the same as during the setup
        # TODO: is this assertion always intended?
        assert self.ENV_NAME == os.getenv(
            "ENV_NAME"
        ), "Environment of the config file ({}) is not the same as during the setup ({}).".format(
            self.ENV_NAME, os.getenv("ENV_NAME")
        )
        config.custom_content = []
        config.custom_content.append(
            ("accounting_group", self.htcondor_accounting_group)
        )
        config.render_variables["analysis_path"] = os.getenv("ANALYSIS_PATH")
        config.custom_content.append(("Requirements", self.htcondor_requirements))
        config.custom_content.append(("+RemoteJob", self.htcondor_remote_job))
        config.custom_content.append(("universe", self.htcondor_universe))
        config.custom_content.append(("docker_image", self.htcondor_docker_image))
        config.custom_content.append(("+RequestWalltime", self.htcondor_walltime))
        config.custom_content.append(("x509userproxy", self.htcondor_user_proxy))
        config.custom_content.append(("request_cpus", self.htcondor_request_cpus))
        # Only include "request_gpus" if any are requested, as nodes with GPU are otherwise excluded
        if float(self.htcondor_request_gpus) > 0:
            config.custom_content.append(("request_gpus", self.htcondor_request_gpus))
        config.custom_content.append(("RequestMemory", self.htcondor_request_memory))
        config.custom_content.append(("RequestDisk", self.htcondor_request_disk))

        # Ensure tarball dir exists
        if not os.path.exists("tarballs/{}".format(self.production_tag)):
            os.makedirs("tarballs/{}".format(self.production_tag))
        # Get time of previous tarballs 
        prev_tar_times_file = os.path.join(os.getenv("ANALYSIS_DATA_PATH"), "tar_times.yaml")
        if not os.path.isfile(prev_tar_times_file):
            old_dict = {}
        else:
            with open(prev_tar_times_file) as f:
                old_dict = yaml.load(f, Loader=yaml.FullLoader)
        # Repack tarball if it is not available local, 
        # remote or if replace_processor_tar is set
        repack_tar = True
        if(os.path.isfile("tarballs/{tag}/{task}_processor.tar.gz".format(
                tag=self.production_tag, task=task_name
            ))
            and not self.replace_processor_tar
        ):
            old_tarball = None
            if task_name in old_dict.keys():
                if self.production_tag in old_dict[task_name].keys():
                    old_time = old_dict[task_name][self.production_tag]
                    old_tarball = law.wlcg.WLCGFileTarget(
                        "{task}/{tag}/job_tarball/{old_time}/processor.tar.gz".format(
                            task=self.__class__.__name__,
                            tag=self.production_tag, 
                            old_time=old_time
                        )
                    )
                    if old_tarball.exists():
                        # If file is found no new tarball necessary
                        repack_tar = False
                        tarball = old_tarball
        if repack_tar:
            # Make new tarball 
            prevdir = os.getcwd()
            os.system("cd $ANALYSIS_PATH")         
            command = [
                "tar",
                "--exclude",
                "*.pyc",
                "-czf",
                "tarballs/{}/{}_processor.tar.gz".format(
                    self.production_tag, task_name
                ),
                "processor",
                "lawluigi_configs/{}_luigi.cfg".format(analysis_name),
                "lawluigi_configs/{}_law.cfg".format(analysis_name),
                "law",
            ]
            code, out, error = interruptable_popen(
                command,
                stdout=PIPE,
                stderr=PIPE,
            )
            if code != 0:
                console.log("Error when taring job {}".format(error))
                console.log("Output: {}".format(out))
                console.log("tar returned non-zero exit status {}".format(code))
                console.rule()
                os.remove(
                    "tarballs/{}/{}_processor.tar.gz".format(
                        self.production_tag, task_name
                    )
                )
                raise Exception("tar failed")
            else:
                console.rule("Successful tar!")
            tarball = law.wlcg.WLCGFileTarget(
                path=self.remote_path(
                    "{tag}/job_tarball/{time}/processor.tar.gz".format(
                        tag=self.production_tag, time=start_time
                    )
                )
            )
            # Copy new tarball to remote
            tarball.parent.touch()
            tarball.copy_from_local(
                src="tarballs/{}/{}_processor.tar.gz".format(
                    self.production_tag, task_name
                )
            )
            console.rule("Tarball uploaded!")
            os.chdir(prevdir)
            # Update time of tarball creation
            new_dict = old_dict
            if task_name not in new_dict.keys():
                new_dict[task_name] = {}
            new_dict[task_name][self.production_tag] = start_time
            with open(prev_tar_times_file, 'w') as f: 
                yaml.dump(new_dict, f)
        # Check if env was found in cvmfs
        env_is_in_cvmfs = os.getenv("CVMFS_ENV_PRESENT")
        if env_is_in_cvmfs == "False":
            # IMPORTANT: environments have to be named differently with each change
            #            as chaching prevents a clean overwrite of existing files
            tarball_env = law.wlcg.WLCGFileTarget(
                path="env_tarballs/{env}_env.tar.gz".format(
                    env=self.ENV_NAME
                )
            )
            if not tarball_env.exists():
                tarball_env.parent.touch()
                tarball_env.copy_from_local(
                    src="tarballs/{}_env.tar.gz".format(self.ENV_NAME)
                )
        # Make string with environmental variables needed for the job-setup
        environment_string = ""
        environment_string += "ENV_NAME={} ".format(self.ENV_NAME)
        environment_string += "ANA_NAME={} ".format(os.getenv("ANA_NAME"))
        environment_string += "USER={} ".format(os.getenv("USER"))
        environment_string += "TARBALL_PATH={} ".format(
            os.path.expandvars(self.wlcg_path) + tarball.path
        )
        # Include path to env tarball if env not in cvmfs
        if env_is_in_cvmfs == "False":
            environment_string += "TARBALL_ENV_PATH={} ".format(
                os.path.expandvars(self.wlcg_path) + tarball_env.path
            )
        config.custom_content.append(("environment", '"' + environment_string + '"'))
        return config
