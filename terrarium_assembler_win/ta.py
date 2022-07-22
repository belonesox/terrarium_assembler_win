"""Main module."""

import argparse
import io
import os
import pathlib
import subprocess
import shutil
import sys
from tempfile import mkstemp
import stat
import re
import yaml
import dataclasses as dc
import datetime
import tarfile
import hashlib 
import time
import json
import csv

from .wheel_utils import parse_wheel_filename
from .utils import *
from .nuitkaflags import *


def write_doc_table(filename, headers, rows):
    with open(filename, 'w', encoding='utf-8') as lf:
        lf.write(f"""
<table class='wikitable' border=1>
""")            
        lf.write(f"""<tr>""")
        for col_ in headers:
            lf.write(f"""<th>{col_}</th>""")
        lf.write(f"""</tr>\n""")
        for row_ in rows:
            lf.write(f"""<tr>""")
            for col_ in row_:
                lf.write(f"""<td>{col_}</td>""")
            lf.write(f"""</tr>\n""")
        lf.write(f"""
</table>
""")            
    return

def fix_win_command(scmd):
    '''
    Из-за большего удобства, мы допускаем в YAML описывать некоторые пути через 
    прямые слеши — тогда не надо в кавычки (даже одинарные) заключать, 
    код выглядит более читаемым. Но виндовые пути, особенно при вызове команд 
    должны быть с виндовым обратным слешом, поэтому применяем грубую эвристику
    чтобы починить слеши на месте.

    Это не поможет, если пути ожидаются с обратными слешами где-то в параметрах,
    но часто помогает.
    '''
    if not ' ' in scmd:
        return scmd
    path_, otherpart = scmd.split(' ', 1)
    path_ = path_.replace('/', '\\')
    scmd = f'{path_} {otherpart}'
    return scmd

class TerrariumAssembler:
    '''
    Генерация переносимых бинарных дистрибутивов для Python-проектов под Windows
    '''
    
    def __init__(self):
        self.curdir = os.getcwd()
        self.output_dir = os.path.join(self.curdir, 'out')        
        self.root_dir = None
        # self.buildroot_dir = 'C:/docmarking-buildroot'
        self.ta_name = 'terrarium_assembler'

        self.pipenv_dir = ''
        from pipenv.project import Project
        p = Project()
        self.pipenv_dir = p.virtualenv_location

        vars_ = {
        #     'pipenv_dir': self.pipenv_dir,
        #     # 'buildroot_dir': self.buildroot_dir
        }

        ap = argparse.ArgumentParser(description='Create a portable windows application')
        ap.add_argument('--debug', default=False, action='store_true', help='Debug version of release')
        ap.add_argument('--docs', default=False, action='store_true', help='Output documentation version')

        # Основные этапы сборки
        self.stages = {
            'download-utilities' : 'download binary files',
            # 'download-msvc' : 'download MSVC versions',
            'checkout' : 'checkout sources',
            'install-utilities': 'install downloaded utilities',
            'init-env': 'install environment',
            'download-wheels': 'download needed WHL-python packages',
            'build-conanlibs': 'compile conan libraries',
            'build-wheels': 'compile wheels for our python sources',
            'install-wheels': 'Install our and external Python wheels',
            'build-projects': 'Compile Python packages to executable',
            'pack_me' :  'Pack current dir to time prefixed tar.bz2',
            'output' :   'Generate «out» for ditribution',
            'gen-docs' : 'Generate docs about sources/packages',
            'make-iso': 'Make ISO disk from distribution',
        }

        for stage, desc in self.stages.items():
            ap.add_argument('--stage-%s' % stage, default=False, action='store_true', help='Stage for %s ' % desc)

        ap.add_argument('--stage-build-and-pack', default='', type=str, help='Install, build and pack')
        ap.add_argument('--stage-download-all', default=False, action='store_true', help='Download all — sources, packages')
        ap.add_argument('--stage-my-source-changed', default='', type=str, help='Fast rebuild/repack if only pythonsourcechanged')
        ap.add_argument('--stage-all', default='', type=str, help='Install, build and pack')
        ap.add_argument('--stage-pack', default='', type=str, help='Stage pack to given destination directory')
        ap.add_argument('--folder-command', default='', type=str, help='Perform some shell command for all projects')
        ap.add_argument('specfile', type=str, help='Specification File')
        
        self.args = args = ap.parse_args()
        if self.args.stage_all:
            self.args.stage_build_and_pack = self.args.stage_all
            self.args.stage_download_all = True

        if self.args.stage_build_and_pack:
            self.args.stage_install_utilities = True
            self.args.stage_init_env = True
            self.args.stage_build_wheels = True
            self.args.stage_install_wheels = True
            self.args.stage_build_projects = True
            self.args.stage_output = self.args.stage_build_and_pack

        if self.args.stage_my_source_changed:
            self.args.stage_checkout = True
            self.args.stage_download_wheels = True
            self.args.stage_init_env = True
            self.args.stage_build_wheels = True
            self.args.stage_install_wheels = True
            self.args.stage_build_projects = True
            self.args.stage_output = self.args.stage_my_source_changed
            self.args.stage_make_iso = True

        if self.args.stage_download_all:
            self.args.stage_download_rpms = True
            self.args.stage_checkout = True
            self.args.stage_download_wheels = True

        specfile_  = expandpath(args.specfile)
        self.root_dir = os.path.split(specfile_)[0]
        os.environ['TERRA_SPECDIR'] = os.path.split(specfile_)[0]
        self.spec, self.tvars = yaml_load(specfile_, vars_)    
        self.start_dir = os.getcwd()
        pass

    def lines2bat(self, name, lines, stage=None):
        '''
        Записать в батник инструкции сборки, 
        и если соотвествующий этап активирован в опциях командной строки,
        то и выполнить этот командный файл.
        '''
        import stat
        os.chdir(self.curdir)
        fname = name + '.bat'

        with open(os.path.join(fname), 'w', encoding="utf-8") as lf:
            lf.write(f"rem Generated {name} \n")
            if stage:
                desc = self.stages[stage]
                stage_  = stage.replace('_', '-')
                lf.write(f'''
rem Stage "{desc}"
rem  Automatically called when {self.ta_name} --stage-{stage_} "{self.args.specfile}" 
''')
            lf.write(f'''
set PIPENV_VENV_IN_PROJECT=1
set TA_PROJECT_DIR=%~dp0
for /f %%i in ('{self.spec.python_dir}\python -E -m pipenv --venv') do set TA_PIPENV_DIR=%%i
''')

            for k, v in self.tvars.items():
                if type(v) in [type(''), type(1)]:
                    lf.write(f'''set TA_{k}={v}\n''')

            lf.write(f'''
set PYTHONHOME=%TA_python_dir%
''')
            lf.write("\n".join(lines))


        st = os.stat(fname)
        os.chmod(fname, st.st_mode | stat.S_IEXEC)

        if stage:
            param = stage.replace('-', '_')
            option = "stage_" + param
            dict_ = vars(self.args)
            if option in dict_:
                if dict_[option]:
                    print("*"*20)
                    print("Executing ", fname)
                    print("*"*20)
                    os.system(fname)
        pass  


    def generate_checkout_sources(self):
        '''
            Just checking out sources.
            This stage should be done when we have authorization to check them out.
        '''
        if "projects" not in self.spec:
            return

        args = self.args
        lines = []
        lines2 = []

        # Install git lfs for user (need once)
        lfs_install = 'git lfs install'
        lines.append(lfs_install)
        lines2.append(lfs_install)

        in_src = os.path.relpath(self.spec.src_dir, start=self.curdir)
        lines.append(f'mkdir {in_src} ')
        already_checkouted = set()

        for git_url, td_ in self.spec.projects.items():
            git_url, git_branch, path_to_dir_, _ = self.explode_pp_node(git_url, td_)
            if path_to_dir_ not in already_checkouted:
                probably_package_name = os.path.split(path_to_dir_)[-1]
                already_checkouted.add(path_to_dir_)
                path_to_dir = os.path.relpath(path_to_dir_, start=self.curdir)
                newpath = path_to_dir + '.new'
                lines.append(f'rmdir /S /Q "{newpath}"')
                scmd = f'''
git --git-dir=/dev/null clone  {git_url} {newpath} 
pushd {newpath} 
git checkout {git_branch}
git lfs pull
popd
''' 
                lines.append(scmd)

                lines2.append(f'''
pushd "{path_to_dir}"
git config core.fileMode false
git pull
git lfs pull
{self.spec.python_dir}\python -E -m pipenv run pip uninstall  {probably_package_name} -y
{self.spec.python_dir}\python -E -m pipenv run python setup.py develop
popd
''')


                # Fucking https://www.virtualbox.org/ticket/19086 + https://www.virtualbox.org/ticket/8761
                lines.append(f"""
if exist "{newpath}\" (
  rmdir /S /Q  "{path_to_dir}"  
  move "{newpath}" "{path_to_dir}"
)
""")

        self.lines2bat("06-checkout", lines, 'checkout')    
        self.lines2bat("96-pullall", lines2)    
        pass

    def get_all_sources(self):
        # for td_ in self.spec.projects:
        for git_url, td_ in self.spec.projects.items():
            git_url, git_branch, path_to_dir_, _ = self.explode_pp_node(git_url, td_)
            yield git_url, git_branch, path_to_dir_

    def explode_pp_node(self, git_url, td_):
        '''
        Преобразует неоднозначное описание yaml-ноды пакета в git_url и branch

        TODO: переписать, притащено из линуксового TA
        '''    
        git_branch = 'master'
        if 'branch' in td_:
            git_branch = td_.branch

        path_to_dir = os.path.join(self.spec.src_dir, giturl2folder(git_url))
        setup_path = path_to_dir

        # if 'subdir' in td_:
        #     subdir = td_.subdir
        # setup_path = path_to_dir

        return git_url, git_branch, path_to_dir, setup_path


    def generate_build_projects(self):
        '''
        Генерация скриптов бинарной сборки для всех проектов.

        Поддерживается сборка 
        * компиляция проектов MVSC
        * компиляция питон-проектов Nuitkой
        * компиляция JS-проектов (обычно скриптов)
        '''

        if "projects" not in  self.spec:
            return

        args = self.args
        lines = []
        lines2 = []
        bfiles = []
        in_src = os.path.relpath(self.spec.src_dir, start=self.curdir)
        tmpdir = os.path.relpath(self.spec.builds_dir, start=self.curdir)
        
        # os.path.join(self.curdir, 'tmp', 'builds')

        for git_url, td_ in self.spec.projects.items():
            lines = []
            git_url, git_branch, path_to_dir_, _ = self.explode_pp_node(git_url, td_)
            projname_ = os.path.split(path_to_dir_)[-1]
            build_name = 'build_' + projname_
            path_to_dir = os.path.relpath(path_to_dir_, start=self.curdir)
            if 'nuitkabuild' in td_:
                nb_ = td_.nuitkabuild
                srcname = nb_.input_py
                defaultname = os.path.splitext(srcname)[0]
                outputname = defaultname
                if "output" in nb_:
                    outputname = nb_.output

                nuitka_flags = nb_.nuitka_flags

                def inherit_flags(nuitka_flags):
                    if 'inherit' in nuitka_flags:
                        nuitka_flags_inherit = self.spec[nuitka_flags.inherit]
                        # Рекурсивно требуем наследования
                        nuitka_flags_inherit = inherit_flags(nuitka_flags_inherit)
                        # Проверяем, что унаследовались.
                        assert 'inherit' not in nuitka_flags_inherit
                        nfm_ = edict({**nuitka_flags_inherit})
                        for group in nuitka_flags:
                            if group in nfm_:
                                nfm_[group] = list(set(nfm_[group] or []).union(set(nuitka_flags[group] or [])))
                            else:
                                nfm_[group] = nuitka_flags[group]
                        del nfm_['inherit']
                        return nfm_
                    return nuitka_flags    

                if 'snsm' in outputname:
                    wtfff=1

                nuitka_flags = inherit_flags(nuitka_flags)

                # nuitka_flags_inherit = self.spec[nuitka_flags.inherit]
                # # Пока считаем, что наследоваться можно только один раз
                # assert 'inherit' not in nuitka_flags_inherit
                # nfm_ = edict({**nuitka_flags_inherit})
                # for group in nuitka_flags:
                #     if group in nfm_:
                #         nfm_[group] = list(set(nfm_[group]).union(set(nuitka_flags[group])))
                #     else:
                #         nfm_[group] = nuitka_flags[group]
                # del nfm_['inherit']

                nf_ = NuitkaFlags(**nuitka_flags)
                nflags_ = nf_.get_flags(tmpdir, nuitka_flags)

                target_dir = os.path.join(tmpdir, outputname + '.dist')
                target_dir_ = os.path.relpath(target_dir, start=self.curdir)

                src = os.path.join(path_to_dir, srcname)
                flags_ = nflags_

                lines.append(fr'''
rmdir /S /Q %TMP%\gen_py
.venv\Scripts\python.exe -m nuitka {nflags_}  {src} 2>&1 > {build_name}.log
IF %ERRORLEVEL% NEQ 0 EXIT 1
''')
                if defaultname != outputname:
                    lines.append(fr'''
move {tmpdir}\{defaultname}.dist\{defaultname}.exe {tmpdir}\{defaultname}.dist\{outputname}.exe 
''')

                lines.append(fr'''
call "C:\Program Files (x86)\Microsoft Visual Studio\2019\BuildTools\Common7\Tools\VsDevCmd.bat"
editbin /largeaddressaware {tmpdir}\{defaultname}.dist\{outputname}.exe 
''')

                lines.append(fr'''
.venv\Scripts\python.exe -m pip list > {tmpdir}\{defaultname}.dist\{outputname}-pip-list.txt 
''')

                if 'copy' in nb_:
                    for it_ in nb_.copy:
                        is_file = os.path.splitext(it_)[1] != ''
                        cp_ = 'copy /-y' if is_file else 'xcopy /I /E /Y /D'
                        lines.append(fr'echo n | {cp_} "{it_}" {tmpdir}\{defaultname}.dist')

                if 'copy_and_rename' in nb_:
                    for to_, from_ in nb_.copy_and_rename.items():
                        from_is_file = os.path.splitext(from_)[1] != ''
                        to_ = to_.replace('/', '\\')
                        from_ = from_.replace('/', '\\')
                        to_dir = os.path.split(to_)[0]
                        lines.append(fr'mkdir {tmpdir}\{defaultname}.dist\{to_dir}')
                        cp_ = 'copy /-y' if from_is_file else 'xcopy /I /E /Y /D'
                        scmd = fr'echo n | {cp_} "{from_}" "{tmpdir}\{defaultname}.dist\{to_}"'
                        lines.append(scmd)

            if 'jsbuild' in td_:
                build = td_.jsbuild
                folder_ = path_to_dir_
                if isinstance(build, dict) and 'folder' in build:
                    folder_ = os.path.join(folder_, build.folder)

                outdir_ = fr'{tmpdir}\{projname_}-jsbuild'
                lines.append(fR"mkdir {outdir_}")
                for file_ in os.listdir(folder_):
                    if file_.endswith('.js'):
                        infile = os.path.join(folder_, file_)
                        outfile = os.path.join(outdir_, os.path.splitext(file_)[0] + '.exe')
                        lines.append(fR"""     
C:\Windows\Microsoft.NET\Framework\v4.0.30319\jsc /out:{outfile}  {infile}                            
        """)
                pass

            if 'vsbuild' in td_:
                    build = td_.vsbuild
                    folder_ = path_to_dir_
                    if isinstance(build, dict) and 'folder' in build:
                        folder_ = os.path.join(folder_, build.folder)
                    projectfile_ = build.projfile
                    projectname_ = os.path.splitext(projectfile_)[0]

                    lines.append(R"""
call "C:\Program Files (x86)\Microsoft Visual Studio\2019\BuildTools\Common7\Tools\VsDevCmd.bat"
    """ % vars(self))
                    # if os.path.exists(os.path.join(folder_, 'packages.config')):
                    lines.append(fR"""     
nuget restore -PackagesDirectory {folder_}\..\packages {folder_}\packages.config 
""")
                    if isinstance(build.platforms, list):
                        for platform_ in build.platforms:
                            odir_ = fr"{tmpdir}\{projectname_}-vsbuild\{platform_}"
                            rodir_ = os.path.relpath(odir_, start=folder_)

                            lines.append(fR"""     
msbuild  /p:Configuration="{build.configuration}" /p:Platform="{platform_}" {folder_}\{projectfile_}
msbuild  /p:OutDir="%TA_PROJECT_DIR%{odir_}" /p:Configuration="{build.configuration}" /p:Platform="{platform_}" {folder_}\{projectfile_}
        """)
                    else:
                        platform_ = build.platforms
                        odir_ = fr"{tmpdir}\{projectname_}-vsbuild\{platform_}"
                        rodir_ = os.path.relpath(odir_, start=folder_)
                        lines.append(fR"""     
msbuild  /p:Configuration="{build.configuration}" /p:Platform="{platform_}" {folder_}\{projectfile_}
msbuild  /p:OutDir="%TA_PROJECT_DIR%{odir_}" /p:Configuration="{build.configuration}" /p:Platform="{platform_}" {folder_}\{projectfile_}
    """)

            if lines:
                self.lines2bat(build_name, lines, None)
                bfiles.append(build_name)
            pass

        lines = []
        for b_ in bfiles:
            lines.append("echo ***********Building " + b_ + ' **************\n\r')
            lines.append("CMD /C " + b_ + '.bat' + '\n\r')
        self.lines2bat("40-build-projects", lines, "build-projects")
        pass



    def generate_download(self):
        '''
        Генерация скачивания бинарных утилит. 
        Практически всего необходимого, кроме зависимостей питон-пакетов, это отдельно.
        '''
        root_dir = self.root_dir
        args = self.args
        packages = []
        lines = []

        in_bin = os.path.relpath(self.spec.bin_dir, start=self.curdir)

        def download_to(url_, to_, force_dir=False):
            dir2download = to_
            scmd = f'wget --no-check-certificate -P {dir2download} -c "{url_}" '
            if os.path.splitext(to_) and not force_dir:
                dir2download, filename = os.path.split(to_)
                lines.append(f'mkdir {dir2download}'.replace('/','\\'))
                scmd = f'wget --no-check-certificate -O {dir2download}/{filename} -c "{url_}" '
            lines.append(scmd)


        for to_, nd_ in self.spec.download.items():
            if isinstance(nd_, list):
                for url_ in nd_:
                    download_to(url_, to_, force_dir=True)
            if isinstance(nd_, str):
                download_to(nd_, to_)

        for name_, it_ in self.spec.download_and_install.items():
            if isinstance(it_, dict):
                msvc_components = ''
                if 'download' in it_:
                    download_ = it_.download
                    if isinstance(download_, dict):
                        for to_, nd_ in download_.items():
                            download_to(nd_, to_)
                if 'components' in it_:
                    msvc_components = " ".join(["--add " + comp for comp in it_.components])
                if 'postdownload' in it_:
                    scmd = it_.postdownload.format(**vars())
                    scmd = fix_win_command(scmd)
                    lines.append(scmd)

        self.lines2bat("01-download-utilities", lines, "download-utilities")    
        pass


    def generate_install(self):
        '''
        Генерация командного скрипта установки всего скачанного,
        кроме питон-пакетов.
        '''
        root_dir = self.root_dir
        args = self.args
        packages = []
        lines = []

        in_bin = os.path.relpath(self.spec.bin_dir, start=self.curdir)

        for name_, it_ in self.spec.download_and_install.items():
            if isinstance(it_, dict):
                msvc_components = ''
                artefact = None
                if 'download' in it_:
                    download_ = it_.download
                    if isinstance(download_, dict):
                        artefact = list(download_.keys())[-1]
                
                if not artefact:
                    continue

                if 'unzip' in it_:
                    to_ = it_.unzip
                    scmd = f'''powershell -command "Expand-Archive -Force '{artefact}'  '{to_}'" '''                    
                    #scmd = f'''tar -xf "{artefact}" --directory "{to_}" '''                    
                    lines.append(scmd)

                if 'unzip7' in it_:
                    to_ = it_.unzip7
                    scmd = f'7z -y x {artefact} -o{to_}'
                    lines.append(scmd)

                if 'target' in it_:
                    to_ = it_.target
                    scmds = f'''
msiexec.exe /I {artefact} /QB-! INSTALLDIR="{to_}" TargetDir="{to_}"
set PATH={to_};%PATH%'''.split('\n')
                    lines += scmds

                if 'components' in it_:
                    msvc_components = " ".join(["--add " + comp for comp in it_.components])

                if 'run' in it_:
                    for line_ in it_.run.split("\n"):
                        scmd = line_.format(**vars())
                        lines.append(fix_win_command(scmd))

        self.lines2bat("02-install-utilities", lines, "install-utilities")    
        pass


    def generate_init_env(self):
        '''
        Генерация командного скрипта инсталляции pipenv-энвайромента
        '''
        root_dir = self.root_dir
        args = self.args
        packages = []
        lines = []

        python_dir = self.spec.python_dir.replace("/", "\\")

        lines.append(fr'''
del /Q Pipfile
{python_dir}\python -E -m pipenv --rm
{python_dir}\python -E -m pipenv --python {self.spec.python_dir}\python.exe        
        ''')

        self.lines2bat("05-init-env", lines, "init-env")    
        pass

    def write_sandbox(self):
        '''
        Генерация Windows-песочницы (облегченной виртуальной машины)
        для чистой сборки в нулевой системе.
        '''
        root_dir = self.root_dir
        wsb_config = fr'''
<Configuration><MappedFolders> 
<MappedFolder><HostFolder>%~dp0</HostFolder> 
<SandboxFolder>C:\Users\WDAGUtilityAccount\Desktop\distro</SandboxFolder> 
<ReadOnly>false</ReadOnly></MappedFolder> 
</MappedFolders> 
<LogonCommand> 
<Command>C:\Users\WDAGUtilityAccount\Desktop\distro\99-install-tools.bat</Command> 
</LogonCommand> 
</Configuration> 
'''
        lines = []


        lines.append(f'''
rem        
setlocal enableDelayedExpansion        
type nul > ta-sandbox.wsb 
''')    
        for line in wsb_config.strip().split("\n"):
            lines.append(f'set "tag_line={line}"')    
            lines.append(f'echo !tag_line! >> ta-sandbox.wsb ')    

        lines.append(f'start ta-sandbox.wsb')    

        self.lines2bat("00-start-sandbox-for-building", lines)


        if not os.path.exists(self.output_dir):
            os.mkdir(self.output_dir)

        scmd = R"""
@"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -InputFormat None -ExecutionPolicy Bypass -Command " [System.Net.ServicePointManager]::SecurityProtocol = 3072; iex ((New-Object System.Net.WebClient).DownloadString('https://chocolatey.org/install.ps1'))" && SET "PATH=%PATH%;%ALLUSERSPROFILE%\chocolatey\bin"
choco install -y far md5sums
choco install -y --allow-downgrade wget --version 1.20.3.20190531
rem choco install -y procmon git windirstat md5sums
rem choco install -y --allow-downgrade wget --version 1.20.3.20190531
"""
        self.lines2bat("99-install-tools", [scmd])

        with open("install-all-wheels.py", "w", encoding='utf-8') as lf:
            lf.write("""
import sys
import os
import glob

wheels_to_install = []

for path_ in sys.argv[1:]:
    for whl in glob.glob(f'{path_}/*.whl'):
        wheels_to_install.append(whl)

wheels = " ".join(wheels_to_install)

scmd = fr'''
{sys.executable} -m pip install --no-deps --force-reinstall --ignore-installed {wheels} 
'''

print(scmd)
os.system(scmd)
""")

        with open("make-iso.py", "w", encoding='utf-8') as lf:
            lf.write("""
import sys
import os

venv_path = os.environ["VIRTUAL_ENV"]
isofilename = os.environ["isofilename"]

scmd = fr'''
{sys.executable} {venv_path}\Scripts\pycdlib-genisoimage -joliet -joliet-long -o out/{isofilename} out/iso
'''
print(scmd)
os.system(scmd)
""")


        scmd = R"""
call 02-install-utilities.bat 
call 15-install-wheels.bat
call 09-build-wheels.bat
call 15-install-wheels.bat
call 40-build-projects.bat
call 50-output.bat
call 51-make-iso.bat
"""
        self.lines2bat("98-install-and-build-for-audit", [scmd])

        lines_ = []
        for git_url, git_branch, path_to_dir_ in self.get_all_sources():
            lines_.append(f'''
@echo ---- Changelog for {path_to_dir_} >> out/%changelogfilename%
git -C {path_to_dir_} log --since="%pyyyy%-%pmm%-%pdd%" --pretty --name-status   >> out/%changelogfilename%
            ''')
        changelog_mode = "\n".join(lines_)


        python_dir = self.spec.python_dir.replace("/", "\\")
        scmd = fR"""
rem
for /f "skip=1" %%x in ('wmic os get localdatetime') do if not defined CurDate set CurDate=%%x
echo %CurDate%
set yyyy=%CurDate:~0,4%
set mm=%CurDate:~4,2%
set dd=%CurDate:~6,2%
set hh=%CurDate:~8,2%
set mi=%CurDate:~10,2%
set ss=%CurDate:~12,2%
set datestr=%yyyy%-%mm%-%dd%-%hh%-%mi%-%ss%
set isoprefix=%datestr%-dm-win-distr
set isofilename=%isoprefix%.iso
set changelogfilename=%isoprefix%.changelog.txt
echo %isofilename% > out/iso/isodistr.txt
rem for /f "tokens=*" %%i in ('dir /b /o:n "out\*.iso"') do @echo %%~ni >> names.txt
for /f "tokens=*" %%i in ('dir /b /o:n "out\*.iso"') do set lastiso=%%~ni 
set /a "pyyyy=%yyyy%-1"
if not defined lastiso set lastiso=%pyyyy%-%mm%-%dd%-%hh%-%mi%-%ss%
set pyyyy=%lastiso:~0,4%
set pmm=%lastiso:~5,2%
set pdd=%lastiso:~8,2%
echo "%pyyyy%-%pmm%-%pdd%"
{changelog_mode}
{python_dir}\python.exe -E -m pipenv run python make-iso.py
@echo ;MD5: >> out/%changelogfilename%
md5sums out/%isofilename% >> out/%changelogfilename%
"""
        self.lines2bat("51-make-iso", [scmd], 'make-iso')
        pass


    def generate_download_wheels(self):
        '''
        Генерация скачивания всех пакетов по зависимостям.
        '''
        os.chdir(self.curdir)

        root_dir = self.root_dir
        args = self.args

        lines = []
        wheel_dir = self.spec.depswheel_dir.replace("/", "\\")
        ourwheel_dir = self.spec.ourwheel_dir.replace("/", "\\")
        lines.append(fr'''
del /q {wheel_dir}\*     
set CONAN_USER_HOME=%~dp0in\libscon
set CONANROOT=%CONAN_USER_HOME%\.conan\data
''')

        paths_ = []
        for pp in self.spec.python_packages:
            # scmd = fr'echo "** Downloading wheel for {pp} **"' 
            # lines.append(scmd)                
            # scmd = fr"{self.spec.python_dir}\python -m pip download {pp} --dest {wheel_dir} " 
            # lines.append(scmd)                
            paths_.append(pp)

        for git_url, td_ in self.spec.projects.items():
            if 'pybuild' not in td_:
                continue

            git_url, git_branch, path_to_dir_, _ = self.explode_pp_node(git_url, td_)
            probably_package_name = os.path.split(path_to_dir_)[-1]
            # path_to_dir = os.path.relpath(path_to_dir_, start=self.curdir)
            # scmd = fr'echo "** Downloading dependend wheels for {path_to_dir} **"' 
            # lines.append(scmd)                
            
            path_ = setup_path = path_to_dir_
            # path_ = os.path.relpath(setup_path, start=self.curdir)

            os.chdir(self.curdir)
            if os.path.exists(setup_path):
                os.chdir(setup_path)
                is_python_package = False
                for file_ in ['setup.py', 'pyproject.toml', 'requirements.txt']:
                    if os.path.exists(file_):
                        is_python_package = True
                        break

                if is_python_package:
                    paths_.append(path_)

            pass

        os.chdir(self.curdir)
        setup_paths = " ".join(paths_)        

        scmd = fr"{self.spec.python_dir}\python -E -m pipenv run pip download {setup_paths} --dest {wheel_dir} --find-links {ourwheel_dir} " 
        lines.append(fix_win_command(scmd))                


        scmd = fr"""
for %%D in ({wheel_dir}\*.tar.*) do {self.spec.python_dir}\python.exe  -E  -m pipenv run pip wheel --no-deps %%D -w {wheel_dir}
del {wheel_dir}\*.tar.*
""" 
        lines.append(scmd)                

        self.lines2bat("09-download-wheels", lines, "download-wheels")
        pass    


    def generate_build_conanlibs(self):
        '''
        Генерация сборки конан пакетов
        '''
        os.chdir(self.curdir)
        lines = []

        python_dir = self.spec.python_dir.replace("/", "\\")
        wheel_dir = self.spec.ourwheel_dir.replace("/", "\\")
        wheelpath = wheel_dir

        relwheelpath = os.path.relpath(wheelpath, start=self.curdir)
        lines.append(fr"""
set PIPENV_PIPFILE=%~dp0Pipfile
set CONAN_USER_HOME=%~dp0in\libscon
set CONANROOT=%CONAN_USER_HOME%\.conan\data
set PYTHONHOME={python_dir}
set PATH=%PYTHONHOME%;%PYTHONHOME%\scripts;C:\Program Files\CMake\bin;%PATH%;
call "C:\Program Files (x86)\Microsoft Visual Studio\2019\BuildTools\Common7\Tools\VsDevCmd.bat"
conan remove  --locks
""")
        for git_url, td_ in self.spec.projects.items():
            if 'conanbuild' not in td_:
                continue

            git_url, git_branch, path_to_dir_, _ = self.explode_pp_node(git_url, td_)
            probably_package_name = os.path.split(path_to_dir_)[-1]
            path_to_dir = os.path.relpath(path_to_dir_, start=self.curdir)
            relwheelpath = os.path.relpath(wheelpath, start=path_to_dir_)

            setup_path = path_to_dir
            scmd = fr'echo "** Building lib for {setup_path} **"' 
            lines.append(scmd)                
            
            setup_path = path_to_dir
            # path_ = os.path.relpath(setup_path, start=self.curdir)
            # if os.path.exists(setup_path):
            scmd = "pushd %s" % (path_to_dir)
            lines.append(scmd)
            relwheelpath = os.path.relpath(wheelpath, start=path_to_dir)
            scmd = fr"conan create . stable/dm -pr:b profile_build -pr:h profile_host -b missing" 
            lines.append(fix_win_command(scmd))                
            lines.append('popd')
            pass
        self.lines2bat("07-build-conanlibs", lines, "build-conanlibs")
        pass


    def generate_build_wheels(self):
        '''
        Генерация сборки пакетов по всем нашим питон модулям.
        '''
        os.chdir(self.curdir)
        lines = []

        python_dir = self.spec.python_dir.replace("/", "\\")
        wheel_dir = self.spec.ourwheel_dir.replace("/", "\\")
        wheelpath = wheel_dir

        relwheelpath = os.path.relpath(wheelpath, start=self.curdir)
        lines.append(fr"""
set PIPENV_PIPFILE=%~dp0Pipfile
set CONAN_USER_HOME=%~dp0in\libscon
set CONANROOT=%CONAN_USER_HOME%\.conan\data
call "C:\Program Files (x86)\Microsoft Visual Studio\2019\BuildTools\Common7\Tools\VsDevCmd.bat"
rmdir /S /Q  {relwheelpath}       
""")
        for git_url, td_ in self.spec.projects.items():
            if 'pybuild' not in td_:
                continue

            git_url, git_branch, path_to_dir_, _ = self.explode_pp_node(git_url, td_)
            probably_package_name = os.path.split(path_to_dir_)[-1]
            path_to_dir = os.path.relpath(path_to_dir_, start=self.curdir)
            relwheelpath = os.path.relpath(wheelpath, start=path_to_dir_)

            setup_path = path_to_dir
            scmd = fr'echo "** Building wheel for {setup_path} **"' 
            lines.append(scmd)                
            
            setup_path = path_to_dir
            path_ = os.path.relpath(setup_path, start=self.curdir)
            if os.path.exists(setup_path):
                scmd = "pushd %s" % (path_to_dir)
                lines.append(scmd)
                relwheelpath = os.path.relpath(wheelpath, start=path_to_dir)
                scmd = fr"{python_dir}\python -E -m pipenv run python setup.py bdist_wheel -d {relwheelpath}" 
                lines.append(fix_win_command(scmd))                
                lines.append('popd')
            pass
        self.lines2bat("08-build-wheels", lines, "build-wheels")
        pass

    def generate_install_wheels(self):
        os.chdir(self.curdir)

        lines = []
        # pl_ = self.get_wheel_list_to_install()

        #--use-feature=2020-resolver
        # scmd = fr'{self.spec.python_dir}/python -m pip install --no-deps --force-reinstall --no-dependencies --ignore-installed  %s ' % (" ".join(pl_))
        # lines.append(fix_win_command(scmd))

        # for p_ in pl_:
        #     scmd = fr'{self.spec.python_dir}/python -m pip install --no-deps --force-reinstall --ignore-installed  %s ' % p_
        #     lines.append(fix_win_command(scmd))

        scmd = fr'{self.spec.python_dir}/python -m pipenv run python install-all-wheels.py {self.spec.extwheel_dir} {self.spec.depswheel_dir} {self.spec.ourwheel_dir} '
        lines.append(fix_win_command(scmd))

        scmd = fr'{self.spec.python_dir}/python -m pipenv run pip list --format json > python-packages.json'
        lines.append(fix_win_command(scmd))

        if 'pipenv_shell_commands' in self.spec:
            for scmd in self.spec.pipenv_shell_commands:
                lines.append(fix_win_command(scmd))

        self.lines2bat("15-install-wheels", lines, "install-wheels")
        pass    


    def get_wheel_list_to_install(self):
        '''
        Выбираем список wheel-пакетов для инсталляции, руководствуясь эвристиками:
        * если несколько пакетов разных версий — берем большую версию (но для пакетов по зависимостям берём меньшую версию)
        * Приоритеты пакетов таковы:
            * скачанные насильно пакеты в extwheel_dir
            * наши пакеты, собранные в ourwheel_dir
            * пакеты, скачанные по зависимостям
        * наши пакеты имеют больший приоритет, перед 
        '''
        from packaging import version        

        os.chdir(self.curdir)

        from enum import Enum, auto

        class WheelVersionPolicy(Enum):
            NEWEST = auto()
            OLDEST = auto()

        def get_wheel_list(wheels_dir, policy=WheelVersionPolicy.NEWEST):
            assert policy in [WheelVersionPolicy.NEWEST,
                              WheelVersionPolicy.OLDEST]
            wheels_dict = {}

            if os.path.exists(wheels_dir):
                for whl in [os.path.join(wheels_dir, whl) 
                                for whl in os.listdir(wheels_dir) 
                                    if whl.endswith('.whl') or whl.endswith('.tar.gz') or whl.endswith('.tar.bz2')]:
                    pw_ = parse_wheel_filename(whl)
                    name_ = pw_.project
                    if name_ not in wheels_dict:
                        wheels_dict[name_] = whl
                    else:
                        whl_version = version.parse(parse_wheel_filename(whl).version)
                        our_version = version.parse(parse_wheel_filename(wheels_dict[name_]).version)
                        if policy == WheelVersionPolicy.NEWEST:
                            replace = whl_version > our_version
                        else:
                            assert policy == WheelVersionPolicy.OLDEST
                            replace = whl_version < our_version
                        if replace:
                            wheels_dict[name_] = whl
            return wheels_dict

        deps_ = get_wheel_list(self.spec.depswheel_dir, policy=WheelVersionPolicy.OLDEST)
        exts_ = get_wheel_list(self.spec.extwheel_dir)
        ours_ = get_wheel_list(self.spec.ourwheel_dir)

        wheels_dict = {**deps_, **exts_, **ours_}

        return list(wheels_dict.values())

    def folder_command(self):
        '''
         Performing same command on all project folders
        '''

        if "projects" not in self.spec:
            return

        in_src = os.path.relpath(self.spec.src_dir, start=self.curdir)
        already_checkouted = set()

        for git_url, td_ in self.spec.projects.items():
            git_url, git_branch, path_to_dir_, _ = self.explode_pp_node(git_url, td_)
            if path_to_dir_ not in already_checkouted:
                probably_package_name = os.path.split(path_to_dir_)[-1]
                already_checkouted.add(path_to_dir_)
                path_to_dir = os.path.relpath(path_to_dir_, start=self.curdir)

                os.chdir(path_to_dir)
                os.system(self.args.folder_command)
                os.chdir(self.curdir)

    def pack_me(self): 
        '''
        Pack sources and deps for audit
        '''   
        time_prefix = datetime.datetime.now().replace(microsecond=0).isoformat().replace(':', '-')
        parentdir, curname = os.path.split(self.curdir)
        disabled_suffix = curname + '.tar.bz2'

        banned_ext = ['.old', '.iso', '.lock', disabled_suffix, '.dblite', '.tmp', '.log']
        banned_start = ['tmp']
        banned_mid = ['/out', '/wtf', '/ourwheel/', '/ourwheel-', '/test.', '/test/', '/.vagrant', '/.vscode', '/key/', '/tmp/', '/src.', '/bin.',  '/cache_', 'cachefilelist_', '/.image', '/!']

        def filter_(tarinfo):
            for s in banned_ext:
                if tarinfo.name.endswith(s):
                    print(tarinfo.name)
                    return None

            for s in banned_start:
                if tarinfo.name.startswith(s):
                    print(tarinfo.name)
                    return None

            for s in banned_mid:
                if s in tarinfo.name:
                    print(tarinfo.name)
                    return None

            return tarinfo          


        tbzname = os.path.join(self.curdir, 
                "%(time_prefix)s-%(curname)s.tar" % vars())
        # tar = tarfile.open(tbzname, "w:bz2")
        tar = tarfile.open(tbzname, "w")
        tar.add(self.curdir, "./sources-for-audit", recursive=True, filter=filter_)
        tar.close()    


    #     tbzname = os.path.join(self.curdir, 
    #             "%(time_prefix)s-%(curname)s.tar.bz2" % vars())
    #     tar = tarfile.open(tbzname, "w:bz2")
    #     tar.add(self.curdir, recursive=True, filter=filter_)
    #     tar.close()    


    def generate_output(self):
        lines = []
        output_ = self.spec.output
        out_dir = output_.distro_dir.replace('/', '\\')
        lines.append(fR'rmdir /S /Q "{out_dir}" ')
        lines.append(fR'mkdir "{out_dir}" ')

        buildroot = self.spec.buildroot_dir
        srcdir  = self.spec.src_dir
        bindir  = self.spec.bin_dir
        for folder, sources_ in output_.folders.items():
            if isinstance(sources_, str):
                sources_ = [s.strip() for s in sources_.strip().split("\n")]
            dst_folder = (out_dir + os.path.sep + folder).replace('/', os.path.sep)
            lines.append(fR"""    
mkdir {dst_folder}
    """)
            for from_ in sources_:
                from__ = from_ 
                from__ = eval(f"fR'{from_}'")            
                if not os.path.splitext(from__)[1]:
                    from__ += R'\*'
                lines.append(fR"""    
echo n | xcopy /I /S /Y  "{from__}" {dst_folder}\
    """)
        self.lines2bat('50-output', lines, 'output')
        pass    

    def gen_docs(self):
        '''
        Генерация некоторой автодокументации
        '''
        root_dir = self.root_dir
        pp_json = 'python-packages.json'
        pp_htm = 'doc-python-packages.htm'
        if os.path.exists(pp_json) and not os.path.exists(pp_htm):
            try:
                json_ = json.loads(open(pp_json, 'r', encoding='utf-8').read())
                rows_ = []
                for r_ in json_:
                    rows_.append([r_['name'], r_['version']])

                write_doc_table(pp_htm, ['Package', 'Version'], sorted(rows_))
            except Exception as ex_:
                print(ex_)
                pass    


        cloc_csv = 'cloc.csv'
        if not os.path.exists(cloc_csv):
            if shutil.which('cloc') and 0:
                os.system(f'cloc ./in/src/ --csv  --report-file="{cloc_csv}" --3')

        if os.path.exists(cloc_csv):
            table_csv = []
            with open(cloc_csv, newline='') as csvfile:
                csv_r = csv.reader(csvfile, delimiter=',', quotechar='|')
                for row in list(csv_r)[1:]:
                    row[-1] = int(float(row[-1]))
                    table_csv.append(row)

            table_csv[-1][-2], table_csv[-1][-1] = table_csv[-1][-1], table_csv[-1][-2]       
            write_doc_table('doc-cloc.htm', ['Файлов', 'Язык', 'Пустых', 'Комментариев', 'Строчек кода', 'Мощность языка', 'COCOMO строк'], 
                            table_csv)        



    def process(self):
        '''
        Основная процедура генерации проекта,
        и возможно его выполнения, если соответствующие опции 
        командной строки активированы.
        '''

        if self.args.folder_command:
            self.folder_command()
            return

        if self.args.stage_pack_me:
            self.pack_me()
            return

        self.gen_docs()

        self.generate_download()
        self.generate_install()
        self.generate_init_env()
        self.generate_checkout_sources()
        self.generate_download_wheels()
        self.generate_build_conanlibs()
        for _ in range(2):
            self.generate_build_wheels()
            self.generate_install_wheels()
        self.generate_build_projects()
        self.generate_output()
        self.write_sandbox()
        pass
