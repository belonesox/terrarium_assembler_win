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

from .wheel_utils import parse_wheel_filename
from .utils import *
from .nuitkaflags import *

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

        vars_ = {
            # 'buildroot_dir': self.buildroot_dir
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
            'download-wheels': 'download needed WHL-python packages',
            'build-wheels': 'compile wheels for our python sources',
            'install-wheels': 'Install our and external Python wheels',
            'build-projects': 'Compile Python packages to executable',
            # 'make-isoexe': 'Also make self-executable install archive and ISO disk',
            # 'pack-me' :  'Pack current dir to time prefixed tar.bz2'
        }

        for stage, desc in self.stages.items():
            ap.add_argument('--stage-%s' % stage, default=False, action='store_true', help='Stage for %s ' % desc)

        ap.add_argument('--stage-build-and-pack', default='', type=str, help='Install, build and pack')
        ap.add_argument('--stage-download-all', default=False, action='store_true', help='Download all — sources, packages')
        ap.add_argument('--stage-my-source-changed', default='', type=str, help='Fast rebuild/repack if only pythonsourcechanged')
        ap.add_argument('--stage-all', default='', type=str, help='Install, build and pack')
        ap.add_argument('--stage-pack', default='', type=str, help='Stage pack to given destination directory')
        ap.add_argument('specfile', type=str, help='Specification File')
        
        self.args = args = ap.parse_args()
        if self.args.stage_all:
            self.args.stage_build_and_pack = self.args.stage_all
            self.args.stage_download_all = True

        if self.args.stage_build_and_pack:
            self.args.stage_install_utilities = True
            self.args.stage_build_wheels = True
            self.args.stage_install_wheels = True
            self.args.stage_build_projects = True
            self.args.stage_pack = self.args.stage_build_and_pack

        if self.args.stage_my_source_changed:
            self.args.stage_checkout = True
            self.args.stage_download_wheels = True
            self.args.stage_build_wheels = True
            self.args.stage_install_wheels = True
            self.args.stage_build_projects = True
            self.args.stage_pack = self.args.stage_my_source_changed

        if self.args.stage_download_all:
            self.args.stage_download_rpms = True
            self.args.stage_checkout = True
            self.args.stage_download_wheels = True

        specfile_  = expandpath(args.specfile)
        self.root_dir = os.path.split(specfile_)[0]
        os.environ['TERRA_SPECDIR'] = os.path.split(specfile_)[0]
        self.spec = yaml_load(specfile_, vars_)    
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


#     def build_projects(self):
#         '''
#         Генерация скриптов бинарной сборки для всех проектов.

#         Поддерживается сборка 
#         * компиляция проектов MVSC
#         * компиляция питон-проектов Nuitkой
#         * компиляция JS-проектов (обычно скриптов)
#         '''
#         if not self.nuitkas:
#             return
#         tmpdir = os.path.join(self.curdir, "tmp/ta")
#         tmpdir_ = os.path.relpath(tmpdir)
#         bfiles = []

#         #First pass
#         module2build = {}
#         standalone2build = []
#         referenced_modules = set()

#         for target_ in self.nuitkas.builds:
#             if 'module' in target_:
#                 module2build[target_.module] = target_
#             else:
#                 standalone2build.append(target_)
#                 if 'modules' in target_:
#                     referenced_modules |= set(target_.modules)
#                     for it_ in target_.modules:
#                         if it_ not in module2build:
#                             module2build[it_] = edict({'module':it_})

#         #processing modules only 

#         for outputname, target_ in module2build.items():
#             block_modules = None
#             if 'block_modules' in target_:
#                 block_modules = target_.block_modules

#             nflags = self.nuitkas.get_flags(os.path.join(tmpdir, 'modules', outputname), target_)
#             if not nflags:
#                 continue
#             target_dir = os.path.join(tmpdir, outputname + '.dist')
#             target_dir_ = os.path.relpath(target_dir, start=self.curdir)
#             target_list = target_dir_.replace('.dist', '.list')
#             tmp_list = '/tmp/module.list'
#             source_dir = dir4mnode(target_)
#             flags_ = ''
#             if 'flags' in target_:
#                 flags_ = target_.flags
#             lines = []
#             build_name = 'build_module_' + outputname
#             nuitka_plugins_dir = self.nuitka_plugins_dir
#             lines.append("""
# export PATH="/usr/lib64/ccache:$PATH"
# find %(source_dir)s -name "*.py" | xargs -i{}  cksum {} > %(tmp_list)s
# if cmp -s %(tmp_list)s %(target_list)s
# then
#     echo "Module '%(outputname)s' looks unchanged" 
# """ % vars())
#             lines.append(R"""
# else
#     nice -19 python3 -m nuitka --include-plugin-directory=%(nuitka_plugins_dir)s %(nflags)s %(flags_)s  2>&1 >%(build_name)s.log
#     RESULT=$?
#     if [ $RESULT == 0 ]; then
#         cp %(tmp_list)s %(target_list)s
#     fi
# fi
# """ % vars())
#             self.fs.folders.append(target_dir)
#             if lines:
#                 self.lines2bat(build_name, lines, None)
#                 bfiles.append(build_name)

#         for target_ in standalone2build:
#             srcname = target_.utility
#             outputname = target_.utility
#             nflags = self.nuitkas.get_flags(tmpdir, target_)
#             target_dir = os.path.join(tmpdir, outputname + '.dist')
#             target_dir_ = os.path.relpath(target_dir, start=self.curdir)
#             src_dir = os.path.relpath(self.src_dir, start=self.curdir)
#             src = os.path.join(src_dir, target_.folder, target_.utility) + '.py'
#             flags_ = ''
#             if 'flags' in target_:
#                 flags_ = target_.flags
#             lines = []
#             lines.append("""
# export PATH="/usr/lib64/ccache:$PATH"
# """ % vars(self))
#             build_name = 'build_' + srcname
#             lines.append(R"""
# time nice -19 python3 -m nuitka  %(nflags)s %(flags_)s %(src)s 2>&1 >%(build_name)s.log
# """ % vars())
#             self.fs.folders.append(target_dir)
#             if "outputname" in target_:
#                 srcname = target_.outputname
#             lines.append(R"""
# mv  %(target_dir_)s/%(outputname)s   %(target_dir_)s/%(srcname)s 
# """ % vars())

#             if "modules" in target_:
#                 force_modules = []
#                 if 'force_modules' in target_:
#                     force_modules = target_.force_modules

#                 for it in target_.modules + force_modules:
#                     mdir_ = None
#                     try:
#                         mdir_ = dir4module(it) 
#                         mdir__ = os.path.relpath(mdir_)
#                         if len(mdir__)<len(mdir_):
#                             mdir_ = mdir__
#                     except:
#                         pass                

#                     try:
#                         mdir_ = module2build[it].folder 
#                     except:
#                         pass                

#                     if mdir_:        
#                         lines.append(R"""
# rsync -rav --exclude=*.py --exclude=*.pyc --exclude=__pycache__ --prune-empty-dirs %(mdir_)s %(target_dir_)s/                
# """ % vars())

#                 force_modules = []
#                 for it in target_.modules:
#                     lines.append(R"""
# rsync -av --include=*.so --include=*.bin --exclude=*  %(tmpdir_)s/modules/%(it)s/ %(target_dir_)s/.                
# rsync -rav  %(tmpdir_)s/modules/%(it)s/%(it)s.dist/ %(target_dir_)s/.                
# """ % vars())
#             self.lines2bat(build_name, lines, None)
#             bfiles.append(build_name)


#         lines = []
#         for b_ in bfiles:
#             lines.append("./" + b_ + '.sh')
#         self.lines2bat("40-build-projectss", lines, "build-projects")
#         pass


    # def generate_file_list_from_pips(self, pips):
    #     '''
    #     Для заданного списка PIP-пакетов, возвращаем список файлов в этих пакетах, которые нужны нам.
    #     '''
    #     file_list = []
    #     pips_ = [p.split('==')[0] for p in pips]
    #     import pkg_resources
    #     for dist in pkg_resources.working_set:
    #         if dist.key in pips_:
    #             if dist.has_metadata('RECORD'):
    #                 lines = dist.get_metadata_lines('RECORD')
    #                 paths = [line.split(',')[0] for line in lines]
    #                 paths = [os.path.join(dist.location, p) for p in paths]
    #                 file_list.extend(paths)

    #     pass
    #     res_ = [x for x in file_list if self.should_copy(x)]
    #     return res_
    #     pass

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
popd
''' 
                lines.append(scmd)

                lines2.append(f'''
pushd "{path_to_dir}"
git config core.fileMode false
git pull
git lfs pull
{self.spec.python_dir}\python -m pip uninstall  {probably_package_name} -y
{self.spec.python_dir}\python setup.py develop
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
        tmpdir = os.path.join(self.spec.buildroot_dir, 'builds')

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
                nuitka_flags_inherit = self.spec[nuitka_flags.inherit]
                # Пока считаем, что наследоваться можно только один раз
                assert 'inherit' not in nuitka_flags_inherit
                nfm_ = edict({**nuitka_flags_inherit})
                for group in nuitka_flags:
                    if group in nfm_:
                        nfm_[group] = list(set(nfm_[group]).union(set(nuitka_flags[group])))
                    else:
                        nfm_[group] = nuitka_flags[group]
                del nfm_['inherit']
                nf_ = NuitkaFlags(**nfm_)
                nflags_ = nf_.get_flags(tmpdir, nfm_)

                target_dir = os.path.join(tmpdir, outputname + '.dist')
                target_dir_ = os.path.relpath(target_dir, start=self.curdir)

                src = os.path.join(path_to_dir, srcname)
                flags_ = nflags_

                lines.append(fr'''
{self.spec.python_dir}\python -m nuitka {nflags_}  {src} 2>&1 > {build_name}.log
''')
                if defaultname != outputname:
                    lines.append(fr'''
move {tmpdir}\{defaultname}.dist\{defaultname}.exe {tmpdir}\{defaultname}.dist\{outputname}.exe 
''')

                lines.append(fr'''
{self.spec.python_dir}\python -m pip freeze > {tmpdir}\{defaultname}.dist\{outputname}-pip-freeze.txt 
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
                    if isinstance(build.platforms, list):
                        for platform_ in build.platforms:
                            odir_ = fr"{tmpdir}\{projectname_}-vsbuild\{platform_}"
                            lines.append(fR"""     
msbuild  /p:Configuration="{build.configuration}" /p:Platform="{platform_}" {folder_}\{projectfile_}
msbuild  /p:OutputPath="{odir_}" /p:OutDir="{odir_}\\" /p:Configuration="{build.configuration}" /p:Platform="{platform_}" {folder_}\{projectfile_}
        """)
                    else:
                        platform_ = build.platforms
                        odir_ = fr"{tmpdir}\{projectname_}-vsbuild\{platform_}"
                        lines.append(fR"""     
msbuild  /p:Configuration="{build.configuration}" /p:Platform="{platform_}" {folder_}\{projectfile_}
msbuild  /p:OutputPath="{odir_}" /p:OutDir="{odir_}\\" /p:Configuration="{build.configuration}" /p:Platform="{platform_}" {folder_}\{projectfile_}
    """)

            if lines:
                self.lines2bat(build_name, lines, None)
                bfiles.append(build_name)
            pass

        lines = []
        for b_ in bfiles:
            lines.append("call " + b_ + '.bat')
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
            scmd = f'wget --no-check-certificate -P {dir2download} -c {url_} '
            if os.path.splitext(to_) and not force_dir:
                dir2download, filename = os.path.split(to_)
                lines.append(f'mkdir {dir2download}'.replace('/','\\'))
                scmd = f'wget --no-check-certificate -O {dir2download}/{filename} -c {url_} '
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

        self.lines2bat("02-download-utilities", lines, "download-utilities")    
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

    def write_sandbox(self):
        '''
        Генерация Windows-песочницы (облегченной виртуальной машины)
        для чистой сборки в нулевой системе.
        '''
        root_dir = self.root_dir
        with open('ta-sandbox.wsb', 'wt', encoding='utf-8') as lf:
            lf.write(fr'''
<Configuration><MappedFolders> 
<MappedFolder><HostFolder>{self.root_dir}</HostFolder> 
<SandboxFolder>C:\Users\WDAGUtilityAccount\Desktop\distro</SandboxFolder> 
<ReadOnly>false</ReadOnly></MappedFolder> 
<MappedFolder><HostFolder>{self.root_dir}\out</HostFolder> 
<SandboxFolder>C:\Users\WDAGUtilityAccount\Desktop\out</SandboxFolder> 
<ReadOnly>false</ReadOnly></MappedFolder> 
</MappedFolders> 
<LogonCommand> 
<Command>C:\Users\WDAGUtilityAccount\Desktop\distro\99-install-tools.bat</Command> 
</LogonCommand> 
</Configuration> 
''')
        if not os.path.exists(self.output_dir):
            os.mkdir(self.output_dir)
        
        scmd = R"""
@"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -InputFormat None -ExecutionPolicy Bypass -Command " [System.Net.ServicePointManager]::SecurityProtocol = 3072; iex ((New-Object System.Net.WebClient).DownloadString('https://chocolatey.org/install.ps1'))" && SET "PATH=%PATH%;%ALLUSERSPROFILE%\chocolatey\bin"
choco install -y far procmon wget 
""" 
        self.lines2bat("99-install-tools", [scmd])
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
        lines.append(fr'''
rmdir /S /Q  {wheel_dir}\*        
''')

        for pp in self.spec.python_packages:
            scmd = fr'echo "** Downloading wheel for {pp} **"' 
            lines.append(scmd)                
            scmd = fr"{self.spec.python_dir}\python -m pip download {pp} --dest {wheel_dir} " 
            lines.append(scmd)                

        for git_url, td_ in self.spec.projects.items():
            if 'pybuild' not in td_:
                continue

            git_url, git_branch, path_to_dir_, _ = self.explode_pp_node(git_url, td_)
            probably_package_name = os.path.split(path_to_dir_)[-1]
            path_to_dir = os.path.relpath(path_to_dir_, start=self.curdir)
            scmd = fr'echo "** Downloading dependend wheels for {path_to_dir} **"' 
            lines.append(scmd)                
            
            setup_path = path_to_dir
            path_ = os.path.relpath(setup_path, start=self.curdir)
            if os.path.exists(setup_path):
                scmd = fr"{self.spec.python_dir}\python -m pip download {setup_path} --dest {wheel_dir} " 
                lines.append(fix_win_command(scmd))                
            pass

        self.lines2bat("07-download-wheels", lines, "download-wheels")
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
        lines.append(fr"rmdir /S /Q  {relwheelpath}\*.*")

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
                scmd = fr"{python_dir}\python setup.py bdist_wheel -d {relwheelpath} " 
                lines.append(fix_win_command(scmd))                
                lines.append('popd')
            pass
        self.lines2bat("09-build-wheels", lines, "build-wheels")
        pass

    def generate_install_wheels(self):
        os.chdir(self.curdir)

        lines = []
        pl_ = self.get_wheel_list_to_install()

        #--use-feature=2020-resolver
        scmd = fr'{self.spec.python_dir}/python -m pip install --no-deps --force-reinstall --no-dependencies --ignore-installed  %s ' % (" ".join(pl_))
        lines.append(fix_win_command(scmd))

        for p_ in pl_:
            scmd = fr'{self.spec.python_dir}/python -m pip install --no-deps --force-reinstall --ignore-installed  %s ' % p_
            lines.append(fix_win_command(scmd))

        self.lines2bat("15-install-wheels", lines, "install-wheels")
        pass    


    def get_wheel_list_to_install(self):
        '''
        Выбираем список wheel-пакетов для инсталляции, руководствуясь эвристиками:
        * если несколько пакетов разных версий — берем большую версию (но для пакетов по зависимостям берем меньшую версию)
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


    # def pack_me(self):    
    #     time_prefix = datetime.datetime.now().replace(microsecond=0).isoformat().replace(':', '-')
    #     parentdir, curname = os.path.split(self.curdir)
    #     disabled_suffix = curname + '.tar.bz2'

    #     banned_ext = ['.old', '.iso', disabled_suffix]
    #     banned_start = ['tmp']
    #     banned_mid = ['/out/', '/wtf/', '/.vagrant/', '/.git/']

    #     def filter_(tarinfo):
    #         for s in banned_ext:
    #             if tarinfo.name.endswith(s):
    #                 print(tarinfo.name)
    #                 return None

    #         for s in banned_start:
    #             if tarinfo.name.startswith(s):
    #                 print(tarinfo.name)
    #                 return None

    #         for s in banned_mid:
    #             if s in tarinfo.name:
    #                 print(tarinfo.name)
    #                 return None

    #         return tarinfo          


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
        self.lines2bat('50-output', lines)
        pass    



    def process(self):
        '''
        Основная процедура генерации проекта,
        и возможно его выполнения, если соответствующие опции 
        командной строки активированы.
        '''
        self.write_sandbox()
        self.generate_build_projects()
        self.generate_download()
        self.generate_install()
        self.generate_checkout_sources()
        self.generate_download_wheels()
        for _ in range(2):
            self.generate_build_wheels()
            self.generate_install_wheels()
        self.generate_output()
        pass
