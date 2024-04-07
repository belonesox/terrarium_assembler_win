"""Main module."""

import argparse
import os
import subprocess
import shutil
import sys
from tempfile import mkstemp
import re
import yaml
import dataclasses as dc
import json
import csv
import requirements

from .wheel_utils import parse_wheel_filename
from .utils import *
from .nuitkaflags import *
from pathlib import Path, PurePath

DEBUG = False
if 'debugpy' in sys.modules:
    DEBUG = True

MAGIC_TO_SELF_ELEVATE="""
FSUTIL DIRTY query %SystemDrive% >NUL || (
    PowerShell "Start-Process -FilePath cmd.exe -Args '/C CHDIR /D %CD% & "%0"' -Verb RunAs"
    EXIT
)
"""

INSTALL_ALL_WHEELS_SCRIPT="install-all-wheels.py"

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
        self.root_dir = None
        # self.buildroot_dir = 'C:/docmarking-buildroot'
        self.ta_name = 'terrarium_assembler'

        self.pipenv_dir = ''
        from pipenv.project import Project
        p = Project()
        self.pipenv_dir = p.virtualenv_location
        self.need_pips = ['pip-audit', 'pipdeptree', 'ordered-set', 'cyclonedx-bom']

        vars_ = {
        #     'pipenv_dir': self.pipenv_dir,
        #     # 'buildroot_dir': self.buildroot_dir
        }

        ap = argparse.ArgumentParser(description='Create a portable windows application')
        ap.add_argument('--debug', default=False, action='store_true', help='Debug version of release')
        ap.add_argument('--docs', default=False, action='store_true', help='Output documentation version')

        self.stages_names = sorted([method_name for method_name in dir(self) if method_name.startswith('stage_')])
        self.stage_methods = [getattr(self, stage_) for stage_ in self.stages_names]

        self.stages = {}
        for s_, sm_ in zip(self.stages_names, self.stage_methods):
            self.stages[fname2stage(s_)] = sm_.__doc__.strip()

        for stage, desc in self.stages.items():
            ap.add_argument(f'--{fname2option(stage)}', default=False,
                            action='store_true', help=f'{desc}')

        ap.add_argument('--folder-command', default='', type=str, help='Perform some shell command for all projects')
        ap.add_argument('--git-sync', default='', type=str, help='Perform lazy git sync for all projects')
        ap.add_argument('--steps', type=str, default='', help='Steps like page list or intervals')
        ap.add_argument('--skip-words', type=str, default='', help='Skip steps that contain these words (comma, separated)')
        ap.add_argument('specfile', type=str, help='Specification File')


        complex_stages = {
            "stage-all": lambda stage: fname2num(stage)>0 and fname2num(stage)<60 and not 'audit' in stage,
            "stage-rebuild": lambda stage: fname2num(stage)>0 and fname2num(stage)<60 and not 'checkout' in stage and not 'download' in stage and not 'audit' in stage,
        }

        for cs_, filter_ in complex_stages.items():
            desc = []
            selected_stages_ = [fname2stage(s_).replace('_', '-') for s_ in self.stages_names if filter_(s_)]
            desc = ' + '.join(selected_stages_)
            ap.add_argument(f'--{cs_}', default=False, action='store_true', help=f'{desc}')


        self.args = args = ap.parse_args()

        if args.steps:
            for step_ in args.steps.split(','):
                if '-' in step_:
                    sfrom, sto = step_.split('-')
                    for s_ in self.stages_names:
                        if int(sfrom) <= fname2num(s_) <= int(sto):
                            setattr(self.args, fname2stage(s_).replace('-','_'), True)
                else:
                    for s_ in self.stages_names:
                        if fname2num(s_) == int(step_):
                            setattr(self.args, fname2stage(s_).replace('-','_'), True)

        for cs_, filter_ in complex_stages.items():
            if vars(self.args)[cs_.replace('-','_')]:
                for s_ in self.stages_names:
                    if filter_(s_):
                        setattr(self.args, fname2stage(s_).replace('-','_'), True)

        if args.skip_words:
            for word_ in args.skip_words.split(','):
                for s_ in self.stages_names:
                    if word_ in s_:
                        setattr(self.args, fname2stage(s_).replace('-','_'), False)


        specfile_  = expandpath(args.specfile)
        self.root_dir = os.path.split(specfile_)[0]
        os.environ['TERRA_SPECDIR'] = os.path.split(specfile_)[0]
        self.spec, self.tvars = yaml_load(specfile_, vars_)
        self.out_dir = 'out'
        if "out_dir" in self.spec:
            self.out_dir = self.spec.out_dir
        self.output_dir = os.path.join(self.curdir, self.out_dir)
        self.start_dir = os.getcwd()

        self.svace_mod = False
        self.svace_path = fr'app\svace\bin\svace.exe'
        if Path(self.svace_path).exists():
            self.svace_mod = True

        Path('reports').mkdir(exist_ok=True, parents=True)
        self.not_linked_python_packages_path = 'tmp/not-linked-python-packages-path.yml'
        self.pip_list_json = 'tmp/pip-list.json'
        self.snapshots_src_path = 'tmp\\snapshots-src'
        self.clean_checkouted_sources_path = 'tmp\\clean-checkouted-sources.zip'
        self.audit_archive_path = 'win-pack-for-audit.zip'
        pass

    def cmd(self, scmd):
        '''
        Print command and perform it.
        May be here we will can catch output and hunt for heizenbugs
        '''
        print(scmd)
        return os.system(scmd)

    def lines2bat(self, name, lines, stage=None):
        '''
        Записать в батник инструкции сборки,
        и если соотвествующий этап активирован в опциях командной строки,
        то и выполнить этот командный файл.
        '''
        import stat
        os.chdir(self.curdir)

        fname = fname2shname(name)
        if stage:
            stage = fname2stage(stage)

        if self.build_mode:
            if stage:
                option = stage.replace('-', '_')
                dict_ = vars(self.args)
                if option in dict_:
                    if dict_[option]:
                        print("*"*20)
                        print("Executing ", fname)
                        print("*"*20)
                        res = self.cmd(fname)
                        failmsg = f'{fname} execution failed!'
                        if res != 0:
                            print(failmsg)
                        assert res==0, 'Execution of stage failed!'
            return


        with open(os.path.join(fname), 'w', encoding="utf-8") as lf:
            lf.write(f"rem Generated {name} \n")
            if stage:
                desc = self.stages[stage]
                stage_  = stage.replace('_', '-')
                lf.write(f'''
rem Stage "{desc}"
rem  Automatically called when {self.ta_name} --stage-{stage_} "{self.args.specfile}"
''')
# for /f %%i in ('{self.spec.python_dir}\python -E -m pipenv --venv') do set TA_PIPENV_DIR=%%i
            lf.write(fr'''
set PIPENV_VENV_IN_PROJECT=1
set TA_PROJECT_DIR=%~dp0
set TA_PIPENV_DIR=%TA_PROJECT_DIR%\.venv
''')

            for k, v in self.tvars.items():
                if type(v) in [type(''), type(1)]:
                    lf.write(f'''set TA_{k}={v}\n''')

            lf.write(f'''
set PYTHONHOME=%TA_python_dir%
''')
            for lines_ in lines:
                for line_ in lines_.split('\n'):
                    if line_:
                        if "elevateme" in line_:
                            lf.write(MAGIC_TO_SELF_ELEVATE)
                        lf.write(f'''{line_}\n''')
                        if line_.strip() and not line_.startswith('for ') and not line_.startswith('set '):
                            lf.write(f'''if %errorlevel% neq 0 exit /b %errorlevel%\n\n''')

            lf.write(f'''
echo "OK with {name}"                     
echo %TIME% %DATE% 
                     
goto :EOF

:error
echo Failed with error #%errorlevel%.
exit /b %errorlevel%
''')

        st = os.stat(fname)
        os.chmod(fname, st.st_mode | stat.S_IEXEC)

        pass


    def stage_06_checkout(self):
        '''
            Checkout sources
        '''
        if "projects" not in self.spec:
            return

        args = self.args
        lines = []
        lines2 = []

        # Install git lfs for user (need once)
        lfs_install = 'git lfs install'
        lines.append(lfs_install)
        # lines2.append(lfs_install)

        # lines.add("rm -rf %s " % in_src)
        lines.append(fr"""
for /f "skip=1" %%x in ('wmic os get localdatetime') do if not defined CurDate set CurDate=%%x
echo %CurDate%
set yyyy=%CurDate:~0,4%
set mm=%CurDate:~4,2%
set dd=%CurDate:~6,2%
set hh=%CurDate:~8,2%
set mi=%CurDate:~10,2%
set ss=%CurDate:~12,2%
set datestr=%yyyy%-%mm%-%dd%-%hh%-%mi%-%ss%

if not exist {self.snapshots_src_path} mkdir {self.snapshots_src_path}
set snapshotdir={self.snapshots_src_path}\snapshot-src-before-%datestr%
if exist {self.spec.src_dir} move {self.spec.src_dir} %snapshotdir%
""")

        in_src = os.path.relpath(self.spec.src_dir, start=self.curdir)
        lines.append(f'if not exist {in_src} mkdir {in_src} ')
        already_checkouted = set()

        for git_url, td_ in self.spec.projects.items():
            git_url, git_branch, path_to_dir_, _ = self.explode_pp_node(git_url, td_)
            if path_to_dir_ not in already_checkouted:
                # probably_package_name = os.path.split(path_to_dir_)[-1]
                already_checkouted.add(path_to_dir_)
                path_to_dir = os.path.relpath(path_to_dir_, start=self.curdir)
                newpath = path_to_dir + '.new'
                lines.append(f'rmdir /S /Q "{newpath}"')
                
                # lines.append('rm -rf "%(newpath)s"' % vars())
                # scmd = 'git --git-dir=/dev/null clone --single-branch --branch %(git_branch)s  --depth=40 %(git_url)s %(newpath)s ' % vars()
                
                scmd = f'''
git --git-dir=/dev/null clone --single-branch --branch {git_branch} --depth=50 {git_url} {newpath}
pushd {newpath}
git checkout {git_branch}
git lfs pull
popd
'''
                lines.append(scmd)

                # Fucking https://www.virtualbox.org/ticket/19086 + https://www.virtualbox.org/ticket/8761
                lines.append(fr"""
if exist "{newpath}\" (
  rmdir /S /Q  "{path_to_dir}"
  move "{newpath}" "{path_to_dir}"
)
""")

        mn_ = get_method_name()
        self.lines2bat(mn_, lines, mn_)
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

        return git_url, git_branch, path_to_dir, setup_path


    def stage_40_build_projects(self):
        '''
        Compile Python/C projects to executable
        '''
        # Генерация скриптов бинарной сборки для всех проектов.

        # Поддерживается сборка
        # * компиляция проектов MVSC
        # * компиляция питон-проектов Nuitkой
        # * компиляция JS-проектов (обычно скриптов)

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
            build_name = 'build-' + projname_.replace('_', '-')
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

                nuitka_flags = inherit_flags(nuitka_flags)

                nf_ = NuitkaFlags(**nuitka_flags)
                nflags_ = nf_.get_flags(tmpdir, nuitka_flags)

                target_dir = os.path.join(tmpdir, outputname + '.dist')

                src = os.path.join(path_to_dir, srcname)
                flags_ = nflags_

                svace_prefix = ''
                if self.svace_mod:
                    build_dir = rf'{tmpdir}\{defaultname}.build'
                    svace_dir = rf'{tmpdir}\{defaultname}.svace-dir'
                    lines.append(fR"""
rmdir /S /Q {build_dir}
                    """)
                    svace_prefix = f'{self.svace_path} build --svace-dir {build_dir} '
                    lines.append(f'''
{self.svace_path} init {build_dir}
    ''')
                    nflags_ = ' --disable-ccache ' + nflags_


                lines.append(fr'''
rmdir /S /Q %TMP%\gen_py
{svace_prefix} .venv\Scripts\python.exe -m nuitka {nflags_}  {src} >{build_name}.log 2>&1
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
                        fdir_ = fr'{tmpdir}\{defaultname}.dist\{to_dir}'
                        lines.append(fr'if not exist {fdir_} mkdir {fdir_}')
                        cp_ = 'copy /-y' if from_is_file else 'xcopy /I /E /Y /D'
                        scmd = fr'echo n | {cp_} "{from_}" "{tmpdir}\{defaultname}.dist\{to_}"'
                        lines.append(scmd)

            if 'jsbuild' in td_:
                build = td_.jsbuild
                folder_ = path_to_dir_
                if isinstance(build, dict) and 'folder' in build:
                    folder_ = os.path.join(folder_, build.folder)

                outdir_ = fr'{tmpdir}\{projname_}-jsbuild'
                lines.append(fR"if not exist {outdir_} mkdir {outdir_}")
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
                    projectfile_ = projname_ + '.sln'
                    if 'projfile' in build:
                        projectfile_ = build.projfile
                    projectname_ = os.path.splitext(projectfile_)[0]

                    lines.append(R"""
call "C:\Program Files (x86)\Microsoft Visual Studio\2019\BuildTools\Common7\Tools\VsDevCmd.bat"
    """ % vars(self))

                    svace_prefix = ''
                    msbuild_flags = ''
                    if self.svace_mod:
                        # msbuild_flags = ' /t:Rebuild '
                        svace_dir = rf'{tmpdir}\{projectname_}'
                        lines.append(fR"""
rmdir /S /Q {svace_dir}\.svace-dir
mkdir {svace_dir}\.svace-dir
{self.svace_path} init {svace_dir}
                        """)
                        svace_prefix = f'{self.svace_path} build --svace-dir {svace_dir} '

                    # if os.path.exists(os.path.join(folder_, 'packages.config')):
                    lines.append(fR"""
if  exist {folder_}\packages.config nuget restore -PackagesDirectory {folder_}\..\packages {folder_}\packages.config | VER>NUL
""")
                    if isinstance(build.platforms, list):
                        for platform_ in build.platforms:
                            odir_ = fr"{tmpdir}\{projectname_}-vsbuild\{platform_}"
                            rodir_ = os.path.relpath(odir_, start=folder_)

                            if self.svace_mod:
                                lines.append(fR"""
msbuild  {msbuild_flags} /t:Clean /p:Configuration="{build.configuration}" /p:Platform="{platform_}" {folder_}\{projectfile_}
rmdir /S /Q "%TA_PROJECT_DIR%{odir_}"
            """)
                            lines.append(fR"""
{svace_prefix} msbuild  {msbuild_flags} /p:Configuration="{build.configuration}" /p:Platform="{platform_}" {folder_}\{projectfile_}
msbuild  {msbuild_flags} /p:OutDir="%TA_PROJECT_DIR%{odir_}" /p:Configuration="{build.configuration}" /p:Platform="{platform_}" {folder_}\{projectfile_}
            """)
                    else:
                        platform_ = build.platforms
                        odir_ = fr"{tmpdir}\{projectname_}-vsbuild\{platform_}"
                        rodir_ = os.path.relpath(odir_, start=folder_)
                        if self.svace_mod:
                            lines.append(fR"""
msbuild  {msbuild_flags} /t:Clean /p:Configuration="{build.configuration}" /p:Platform="{platform_}" {folder_}\{projectfile_}
rmdir /S /Q "%TA_PROJECT_DIR%{odir_}"
        """)
                        lines.append(fR"""
{svace_prefix} msbuild  {msbuild_flags} /p:Configuration="{build.configuration}" /p:Platform="{platform_}" {folder_}\{projectfile_}
msbuild  {msbuild_flags} /p:OutDir="%TA_PROJECT_DIR%{odir_}" /p:Configuration="{build.configuration}" /p:Platform="{platform_}" {folder_}\{projectfile_}
    """)

            if lines:
                self.lines2bat(build_name, lines, None)
                bfiles.append(build_name)
            pass

        lines = []
        for b_ in bfiles:
            lines.append("echo ***********Building " + b_ + ' **************\n\r')
            lines.append("CMD /C ta-" + b_ + '.bat' + '\n\r')
            lines.append(f'''if %errorlevel% neq 0 exit /b %errorlevel%\n\n''')

        mn_ = get_method_name()
        self.lines2bat(mn_, lines, mn_)
        pass



    def stage_01_download_binaries(self):
        '''
        Download binary utilities — compilers, etc
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
                lines.append(f'if not exist {dir2download} mkdir {dir2download}'.replace('/','\\'))
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
                    # elif isinstance(download_, str):
                    #     download_to(nd_, to_)

                if 'components' in it_:
                    msvc_components = " ".join(["--add " + comp for comp in it_.components])
                if 'postdownload' in it_:
                    scmd = it_.postdownload.format(**vars())
                    scmd = fix_win_command(scmd)
                    lines.append(scmd)

        mn_ = get_method_name()
        self.lines2bat(mn_, lines, mn_)
        pass


    def stage_02_install_utilities(self):
        '''
        install downloaded utilities
        '''
        root_dir = self.root_dir
        args = self.args
        packages = []
        lines = []

        Path('ta-if-symlink.ps1').write_text(r'''Get-Item $Env:TA_PROJECT_DIR | Select-Object | foreach {if($_.Target){$_.Target.replace('UNC\', '\\')+'\'}}''')
        lines.append(r'''
rem elevateme
cd %TA_PROJECT_DIR%                     
for /f %%i in ('powershell -executionpolicy bypass -File %TA_PROJECT_DIR%\ta-if-symlink.ps1') do set "TA_SYMLINK_PREFIX=%%i"''')

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
msiexec.exe /I %TA_SYMLINK_PREFIX%{artefact} /QB-! INSTALLDIR="{to_}" TargetDir="{to_}"
set PATH={to_};%PATH%'''.split('\n')
                    lines += scmds

                if 'components' in it_:
                    msvc_components = " ".join(["--add " + comp for comp in it_.components])

                if 'run' in it_:
                    for line_ in it_.run.split("\n"):
                        scmd = line_.format(**vars())
                        lines.append(fix_win_command(scmd))

        mn_ = get_method_name()
        self.lines2bat(mn_, lines, mn_)
        pass


    def stage_05_init_env(self):
        '''
        Create python environment
        '''
        root_dir = self.root_dir
        
        with open(INSTALL_ALL_WHEELS_SCRIPT, "w", encoding='utf-8') as lf:
            lf.write(r"""
import sys
import os
import glob
from pathlib import Path

wheels_to_install = []

for path_ in sys.argv[1:]:
    for whl in glob.glob(f'{path_}/*.whl'):
        wheels_to_install.append(whl)

reqs_path = r'tmp/reqs.txt'
Path(reqs_path).parent.mkdir(exist_ok=True, parents=True)
Path(reqs_path).write_text("\n".join(wheels_to_install))


scmd = fr'''
{sys.executable} -m pip install --no-deps --force-reinstall --ignore-installed -r {reqs_path}
'''

print(scmd)
os.system(scmd)
""")
        
        args = self.args
        packages = []
        lines = []

        python_dir = self.spec.python_dir.replace("/", "\\")
# {python_dir}\python -E -m pipenv --rm | VER>NUL

        lines.append(fr'''
del /Q Pipfile | VER>NUL
rmdir /Q /S .venv | VER>NUL
set PIPENV_PIPFILE=
{python_dir}\python -E -m pipenv --python {self.spec.python_dir}\python.exe
{python_dir}\python -E -m pipenv run python {INSTALL_ALL_WHEELS_SCRIPT} {self.spec.basewheel_dir} '
        ''')

        mn_ = get_method_name()
        self.lines2bat(mn_, lines, mn_)
        pass

    # def stage_91_pack_svace_dirs(self):
    #     '''
    #       Pack .svace-dir from all build directories
    #     '''
    #     # Генерация Windows-песочницы (облегченной виртуальной машины)
    #     # для чистой сборки в нулевой системе.
    #     root_dir = self.root_dir
    #     ...
    #     # svace_dirs = []
    #     svace_dirs = [f.as_posix() for f in Path(self.spec.builds_dir).rglob('**/.svace-dir')]
    #     scmd = '7z a -t7z -m0=BCJ2 -m1=LZMA2:d=1024m -aoa ta-svace-dirs.zip ' + ' '.join(svace_dirs)           
    #     mn_ = get_method_name()
    #     self.lines2bat(mn_, [scmd], mn_)
    #     ...        


#     def stage_96_start_clean_box(self):
#         '''
#           write Vagrant file for Hyper-V internal builder
#         '''
#         # Генерация Windows-песочницы (виртуальной машины)
#         # для чистой сборки в нулевой системе.
#         root_dir = self.root_dir
#         Path('Vagrantfile').write_text(fr'''
# Vagrant.configure(2) do |config|
#   config.vm.box_check_update = false
#   config.vm.synced_folder '.', '/vagrant', disabled: true
 
#   config.vm.define "ta-builder-hyperv" do |conf|
#     vmname = "ta-builder-hyperv"
#     conf.vm.box = "ta-builder-hyperv"
#     conf.vm.box_url = "./in/bin/vagrant-boxes/hyperv/hypev-win.box"
#     config.vm.provider "hyperv" do |h|
#        h.maxmemory = 8192
#        h.linked_clone = true
#        h.cpus = 4    
#        h.enable_virtualization_extensions = true
#        h.vm_integration_services = {{
#           guest_service_interface: true,
#        }}	
#     end
#     conf.vm.synced_folder '.', 'C:\\distro', disabled: false
#   end
# end
# ''')
#         lines = []
#         lines.append(rf'''
# vagrant up                     
# powershell -c "vmconnect.exe $env:computername $(Get-VM -Id $(Get-Content .\.vagrant\machines\ta-builder-hyperv\hyperv\id)).Name"
# ''')
#         mn_ = get_method_name()
#         self.lines2bat(mn_, lines)

#     def stage_97_write_sandbox(self):
#         '''
#           run a windows standbox
#         '''
#         # Генерация Windows-песочницы (облегченной виртуальной машины)
#         # для чистой сборки в нулевой системе.
#         root_dir = self.root_dir
#         wsb_config = fr'''
# <Configuration>
# <MemoryInMB>8192</MemoryInMB>
# <MappedFolders>
# <MappedFolder><HostFolder>%~dp0</HostFolder>
# <SandboxFolder>C:\Users\WDAGUtilityAccount\Desktop\distro</SandboxFolder>
# <ReadOnly>false</ReadOnly></MappedFolder>
# </MappedFolders>
# </Configuration>
# '''

# # <LogonCommand>
# # <Command>C:\Users\WDAGUtilityAccount\Desktop\distro\ta-99-useful-tools.bat</Command>
# # </LogonCommand>

#         lines = []

#         lines.append(f'''
# rem
# setlocal enableDelayedExpansion
# type nul > ta-sandbox.wsb
# ''')
#         for line in wsb_config.strip().split("\n"):
#             lines.append(f'set "tag_line={line}"')
#             lines.append(f'echo !tag_line! >> ta-sandbox.wsb ')

#         lines.append(f'start ta-sandbox.wsb')

#         mn_ = get_method_name()
#         self.lines2bat(mn_, lines)

    def stage_51_make_iso(self):
        '''
          Make ISOs
        '''

        lines_all = []        
        for output_key, output_ in self.spec.outputs.items():
            build_output_name = f'generate-iso-for-{output_key}'
            
            lines_ = []
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
echo %isofilename% > {output_key}/iso/isodistr.txt
for /f "tokens=*" %%i in ('dir /b /o:n "{output_key}\*.iso"') do set lastiso=%%~ni
set /a "pyyyy=%yyyy%-1"
if not defined lastiso set lastiso=%pyyyy%-%mm%-%dd%-%hh%-%mi%-%ss%
set pyyyy=%lastiso:~0,4%
set pmm=%lastiso:~5,2%
set pdd=%lastiso:~8,2%
echo "%pyyyy%-%pmm%-%pdd%"
{changelog_mode}
.venv\Scripts\python .venv\Scripts\pycdlib-genisoimage -U -iso-level 4 -R -o {output_key}/%isofilename% {output_key}/iso
@echo ;MD5: >> {output_key}/%changelogfilename%
md5sums {output_key}/%isofilename% >> {output_key}/%changelogfilename%
del /Q {output_key}\last.iso | VER>NUL
cmd /c "mklink /H {output_key}\last.iso {output_key}\%isofilename%"
"""
            self.lines2bat(build_output_name, [scmd])
            lines_all.append(f'call ta-{build_output_name}.bat')

        mn_ = get_method_name()
        self.lines2bat(mn_, lines_all, mn_)
        pass


    def stage_04_download_base_wheels(self):
        '''
        Download base wheel python packages
        '''
        os.chdir(self.curdir)

        args = self.args

        lines = []
        wheel_dir = self.spec.basewheel_dir.replace("/", "\\")
        lines.append(fr'''
if not exist "{wheel_dir}" mkdir "{wheel_dir}"
del /q {wheel_dir}\* | VER>NUL
set CONAN_USER_HOME=%~dp0{self.spec.libscon_dir}
set CONANROOT=%CONAN_USER_HOME%\.conan\data
''')

        paths_ = []
        for pp in self.spec.python_packages:
            if '==' in pp:
                paths_.append(pp)

        os.chdir(self.curdir)
        setup_paths = " ".join(paths_)

        scmd = fr"{self.spec.python_dir}\python -m pip download {setup_paths} --dest {wheel_dir} "
        lines.append(fix_win_command(scmd))

        if 'remove_python_packages_from_download' in self.spec:
            for package_ in self.spec.remove_python_packages_from_download:
                scmd = fr'''del /Q {wheel_dir}\{package_}-*  | VER>NUL '''        
                lines.append(scmd)                

        scmd = fr"""
for %%D in ({wheel_dir}\*.tar.*) do {self.spec.python_dir}\python.exe  -E  -m pipenv run pip wheel --no-deps %%D -w {wheel_dir}
del /Q {wheel_dir}\*.tar.* | VER>NUL
"""
        lines.append(scmd)

        mn_ = get_method_name()
        self.lines2bat(mn_, lines, mn_)
        pass


    def stage_09_download_wheels(self):
        '''
        Download needed WHL-python packages
        '''
        os.chdir(self.curdir)

        root_dir = self.root_dir
        args = self.args

        lines = []
        wheel_dir = self.spec.depswheel_dir.replace("/", "\\")
        ourwheel_dir = self.spec.ourwheel_dir.replace("/", "\\")
        lines.append(fr'''
del /q {wheel_dir}\* | VER>NUL
set CONAN_USER_HOME=%~dp0{self.spec.libscon_dir}
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
                for file_ in ['setup.py', 'pyproject.toml']:
                    if os.path.exists(file_):
                        is_python_package = True
                        break

                if is_python_package:
                    paths_.append(path_)

                for file_ in ['requirements.txt']:
                    if os.path.exists(file_):
                        paths_.append(fr' -r {setup_path}\{file_}')
                        break


            pass

        os.chdir(self.curdir)
        setup_paths = " ".join(paths_)

        need_pips_str = " ".join(self.need_pips)

        scmd = fr"{self.spec.python_dir}\python -E -m pipenv run pip download {need_pips_str} {setup_paths} --dest {wheel_dir} --find-links {ourwheel_dir} "
        lines.append(fix_win_command(scmd))

        if 'remove_python_packages_from_download' in self.spec:
            for package_ in self.spec.remove_python_packages_from_download:
                scmd = fr'''del /Q {wheel_dir}\{package_}-*  | VER>NUL '''        
                lines.append(scmd)                

        scmd = fr"""
for %%D in ({wheel_dir}\*.tar.*) do {self.spec.python_dir}\python.exe  -E  -m pipenv run pip wheel --no-deps %%D -w {wheel_dir}
del {wheel_dir}\*.tar.*
"""
        lines.append(scmd)

        mn_ = get_method_name()
        self.lines2bat(mn_, lines, mn_)
        pass


    def stage_07_audit_extra_build_conanlibs(self):
        '''
        Compile conan libraries
        '''
        os.chdir(self.curdir)
        lines = []

        python_dir = self.spec.python_dir.replace("/", "\\")
        wheel_dir = self.spec.ourwheel_dir.replace("/", "\\")
        wheelpath = wheel_dir

        relwheelpath = os.path.relpath(wheelpath, start=self.curdir)
        lines.append(fr"""
set PIPENV_PIPFILE=%~dp0Pipfile
set CONAN_USER_HOME=%~dp0{self.spec.libscon_dir}
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

        mn_ = get_method_name()
        self.lines2bat(mn_, lines, mn_)
        pass


    def stage_08_build_wheels(self):
        '''
        Сompile wheels for our python sources
        '''
        os.chdir(self.curdir)
        lines = []

        python_dir = self.spec.python_dir.replace("/", "\\")
        wheel_dir = self.spec.ourwheel_dir.replace("/", "\\")
        wheelpath = wheel_dir

        relwheelpath = os.path.relpath(wheelpath, start=self.curdir)
        lines.append(fr"""
set PIPENV_PIPFILE=%~dp0Pipfile
set CONAN_USER_HOME=%~dp0{self.spec.libscon_dir}
set CONANROOT=%CONAN_USER_HOME%\.conan\data
call "C:\Program Files (x86)\Microsoft Visual Studio\2019\BuildTools\Common7\Tools\VsDevCmd.bat"
rmdir /S /Q  {relwheelpath}
""")
        for git_url, td_ in self.spec.projects.items():
            if 'pybuild' not in td_:
                continue

            if 'pybuild' in td_ and not td_.pybuild:
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
        mn_ = get_method_name()
        self.lines2bat(mn_, lines, mn_)
        pass

    def stage_15_install_wheels(self):
        '''
        Install our and external Python wheels
        '''
        os.chdir(self.curdir)

        lines = []
        # pl_ = self.get_wheel_list_to_install()

        #--use-feature=2020-resolver
        # scmd = fr'{self.spec.python_dir}/python -m pip install --no-deps --force-reinstall --no-dependencies --ignore-installed  %s ' % (" ".join(pl_))
        # lines.append(fix_win_command(scmd))

        # for p_ in pl_:
        #     scmd = fr'{self.spec.python_dir}/python -m pip install --no-deps --force-reinstall --ignore-installed  %s ' % p_
        #     lines.append(fix_win_command(scmd))
# {self.spec.python_dir}\python -E -m pipenv --rm | VER>NUL

        lines.append(fr'''
del /Q Pipfile | VER>NUL
set PIPENV_PIPFILE=
rmdir /Q /S .venv | VER>NUL
{self.spec.python_dir}\python -E -m pipenv --python {self.spec.python_dir}\python.exe
        ''')

        scmd = fr'{self.spec.python_dir}/python -m pipenv run python {INSTALL_ALL_WHEELS_SCRIPT} {self.spec.extwheel_dir} {self.spec.depswheel_dir} {self.spec.ourwheel_dir} '
        lines.append(fix_win_command(scmd))

        scmd = fr'{self.spec.python_dir}/python -m pipenv run pip list --format json > {self.pip_list_json}'
        lines.append(fix_win_command(scmd))

        if 'pipenv_shell_commands' in self.spec:
            for scmd in self.spec.pipenv_shell_commands or []:
                lines.append(fix_win_command(scmd))

        mn_ = get_method_name()
        self.lines2bat(mn_, lines, mn_)
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

        print(f'Running command «{self.args.folder_command}» on all project paths')
        for git_url, td_ in self.spec.projects.items():
            git_url, git_branch, path_to_dir_, _ = self.explode_pp_node(git_url, td_)
            if path_to_dir_ not in already_checkouted:
                probably_package_name = os.path.split(path_to_dir_)[-1]
                already_checkouted.add(path_to_dir_)
                path_to_dir = os.path.relpath(path_to_dir_, start=self.curdir)

                if os.path.exists(path_to_dir):
                    print(f'Running command on path «{path_to_dir}»')
                    os.chdir(path_to_dir)
                    os.system(self.args.folder_command)
                    os.chdir(self.curdir)
                else:
                    print(f'Cannot find path {path_to_dir}')

    def git_sync(self):
        '''
         Performing lazy git sync all project folders
         * get last git commit message (usially link to issue)
         * commit with same message
         * pull-merge (without rebase)
         * push to same branch
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
                print(f'''\nSyncing project "{path_to_dir}"''')
                last_commit_message = subprocess.check_output("git log -1 --pretty=%B", shell=True).decode("utf-8")
                last_commit_message = last_commit_message.strip('"')
                last_commit_message = last_commit_message.strip("'")
                if not last_commit_message.startswith("Merge branch"):
                    os.system(f'''git commit -am "{last_commit_message}" ''')
                os.system(f'''git pull --rebase=false ''')
                if 'out' in self.args.git_sync:
                    os.system(f'''git push origin ''')
                os.chdir(self.curdir)

    def stage_50_output(self):
        '''
        Generate «output folders» for ditribution
        '''
        lines_all = []
        for output_key, output_ in self.spec.outputs.items():
            build_output_name = f'generate-output-folder-for-{output_key}'
            lines = []
            out_dir = output_key.replace('/', '\\') + '\\iso'
            lines.append(fR'rmdir /S /Q "{out_dir}" ')
            lines.append(fR'if not exist "{out_dir}" mkdir "{out_dir}" ')

            buildroot = self.spec.buildroot_dir
            srcdir  = self.spec.src_dir
            bindir  = self.spec.bin_dir
            folders = output_.folders
            for folder, sources_ in folders.items():
                if isinstance(sources_, str):
                    folders[folder] = [sources_]
                    
            if 'inherit' in output_:
                for k, v in self.spec.outputs[output_.inherit].folders.items():
                    if k not in folders:
                        folders[k] = v
                    else:    
                        if isinstance(v, str):
                            v = [v]
                        folders[k] = sorted( set(folders[k]) | set(v) ) 
            # folders = dict(**self.spec.outputs[output_.inherit].folders)
            # folders = {**folders, **output_.folders} 
            
            for folder, sources_ in folders.items():
                # if isinstance(sources_, str):
                #     sources_ = [s.strip() for s in sources_.strip().split("\n")]
                dst_folder = (out_dir + os.path.sep + folder).replace('/', os.path.sep)
                lines.append(fR"""
    if not exist "{dst_folder}" mkdir "{dst_folder}"
        """)
                for from_ in sources_:
                    from__ = from_
                    from__ = eval(f"fR'{from_}'")
                    if not os.path.splitext(from__)[1]:
                        from__ += R'\*'
                    lines.append(fR"""
    echo n | xcopy /I /S /Y  "{from__}" {dst_folder}\
        """)
                    
            self.lines2bat(build_output_name, lines)
            lines_all.append(f'call ta-{build_output_name}.bat')

        mn_ = get_method_name()
        self.lines2bat(mn_, lines_all, mn_)
        pass

    def stage_90_audit_analyse(self):
        '''
        Generate some documentantion about distro
        '''
        if not self.build_mode:
            mn_ = get_method_name()
            lines = [
                f'''
{sys.executable} {sys.argv[0]}-script.py "{self.args.specfile}" --stage-audit-analyse
                ''']
            self.lines2bat(mn_, lines, mn_)
            return

        if not self.args.stage_audit_analyse:
            return
        
        wiki_defines_lines = []
        for k, v in  [(path_var, getattr(self.spec, path_var)) for path_var in vars(self.spec) if '_path' in path_var or '_dir' in path_var]:
            wiki_defines_lines.append(f'''{{{{#vardefine:{k}|{v}}}}}''')

        Path('reports/wiki-defines.wiki').write_text(' '.join([''] + sorted(list(wiki_defines_lines))))

        def analyze_venv():
            cyclone_json = 'tmp/cyclonedx-bom.json'
            if Path(cyclone_json).exists():
                Path(cyclone_json).unlink()
            for scmd in f'''
./.venv/Scripts/pip-audit -o tmp/pip-audit-report.json -f json | VER>NUL
./.venv/Scripts/pipdeptree --json > tmp/pipdeptree.json
./.venv/Scripts/python -m pip list --format freeze > tmp/piplist-freeze.txt
./.venv/Scripts/cyclonedx-py --format json -r -i tmp/piplist-freeze.txt  -o tmp/cyclonedx-bom.json
'''.strip().split('\n'):
                self.cmd(fix_win_command(scmd))
    # rm -f tmp/cyclonedx-bom.json

        def generate_docs_graps():
            lines = [f'''
            digraph G {{
                rankdir=LR;
                ranksep=1;
                node[shape=box3d, fontsize=8, fontname=Calibry, style=filled fillcolor=aliceblue];
                edge[color=blue, fontsize=6, fontname=Calibry, style=dashed, dir=back];
            ''']
            json_ = json.loads(open('tmp/pipdeptree.json').read())
            # temporary hack.
            # todo: later we need to rewrite the code, deleting autoorphaned deps from auxiliary packages such as Nuitka
            ignore_packages = set('''pipdeptree
pip pip-api
Jinja2 MarkupSafe
Nuitka zstandard
'''.split() + self.need_pips
)

# Nuitka cyclonedx-python-lib py-serializeable
#     defusedxml sortedcontainers packageurl-python py-serializable toml SCons license-expression boolean.py filelock  pip pip-api
#     rich Pygments markdown-it-py mdurl Jinja2 MarkupSafe

            our_packages = set()
            for whl in Path(self.spec.ourwheel_dir).rglob('*.whl'):
                package_name = whl.stem.lower().split('-')[0].replace('_', '-')
                our_packages.add(package_name)

            linked_packages = set()
            for v1_ in json_:
                package_ = v1_['package']
                deps_ = v1_['dependencies']
                key1_  = package_['key']
                name1_ = package_['package_name']
                if name1_ not in ignore_packages:
                    for v2_ in deps_:
                        linked_packages.add(key1_)
                        key2_  = v2_['key']
                        name2_ = v2_['package_name']
                        if name2_ not in ignore_packages:
                            linked_packages.add(key2_)

            known_packages = set()
            not_linked_packages = set()
            for r_ in json_:
                package_ = r_['package']
                key_  = package_['key']
                if key_ not in linked_packages:
                    not_linked_packages.add(key_)
                    continue
                name_ = package_['package_name']
                if name_ not in ignore_packages:
                    known_packages.add(name_.lower())
                    fillcolormod = ''
                    if name_ in our_packages:
                        fillcolormod = 'fillcolor=cornsilk '
                    lines.append(f''' "{key_}" [label="{name_}" {fillcolormod}]; ''')

            with open(self.not_linked_python_packages_path, 'w') as lf:
                lf.write(yaml.dump(not_linked_packages))

            for v1_ in json_:
                package_ = v1_['package']
                deps_ = v1_['dependencies']
                key1_  = package_['key']
                if key1_ not in linked_packages:
                    continue
                name1_ = package_['package_name']
                if key1_ == 'pip-audit':
                    wtf = 1
                if name1_ not in ignore_packages:
                    for v2_ in deps_:
                        key2_  = v2_['key']
                        name2_ = v2_['package_name']
                        if name2_ not in ignore_packages:
                            lines.append(f''' "{key1_}" -> "{key2_}" ;''')

            for git_url, td_ in self.spec.projects.items():
                git_url, git_branch, path_to_dir_, _ = self.explode_pp_node(git_url, td_)
                projname_ = os.path.split(path_to_dir_)[-1]
                path_to_dir = os.path.relpath(path_to_dir_, start=self.curdir)
                if 'nuitkabuild' in td_:
                    nb_ = td_.nuitkabuild
                    srcname = nb_.input_py
                    defaultname = os.path.splitext(srcname)[0]
                    outputname = defaultname
                    if "output" in nb_:
                        outputname = nb_.output
                    src = os.path.join(path_to_dir, srcname)

                    folder_ = path_to_dir
                    utility_ = outputname
                    lines.append(f''' "{utility_}-tool" [label="{utility_}" shape=note fillcolor=darkseagreen2] ;''')
                    # folderfullpath_ = Path(self.src_dir) / folder_

                    if not Path(src).exists():
                        continue

                    code_ = open(src, 'r', encoding='utf-8').read()

                    imported_modules = set()
                    for module_ in generate_imports_from_python_file(code_, path_to_dir):
                        name_ = module_.replace('_', '-')
                        if name_ == 'trans':
                            wtf = 1
                        if name_ in known_packages:
                            imported_modules.add(name_)

                    for module_ in sorted(list(imported_modules)):
                        lines.append(f''' "{utility_}-tool" -> "{module_}" [style=dotted] ;''')

                    reqs = Path(path_to_dir) / 'requirements.txt'
                    if reqs.exists():
                        with open(reqs, 'r', encoding='utf-8') as fd:
                            try:
                                parsed_ = requirements.parse(fd)
                                for req in parsed_:
                                    lines.append(f''' "{utility_}-tool" -> "{req.name}" [style=dotted] ;''')
                            except Exception as ex_:
                                print(f'Failed to parse {reqs}')
                                print(ex_)

            lines.append('}')

            with open('reports/pipdeptree.dot', 'w') as lf:
                lf.write('\n'.join(lines))

            self.cmd(f'''
dot -Tsvg reports/pipdeptree.dot > reports/pipdeptree.svg 
''')
            ...
                
        if DEBUG:
            analyze_venv()
            generate_docs_graps()
        else:    
            try:
                analyze_venv()
                generate_docs_graps()
            except Exception as ex_:
                print(ex_)

        try:
        # if 1:
            json_ = json.loads(open('tmp/pip-audit-report.json').read())
            rows_ = []
            for r_ in json_['dependencies']:
                if 'vulns' in r_:
                    for v_ in r_['vulns']:
                        rows_.append([r_['name'], r_['version'], v_['id'], ','.join(v_['fix_versions']), v_['description']])

            write_doc_table('reports/pip-audit-report.htm', ['Пакет', 'Версия', 'Возможная уязвимость', 'Исправлено в версиях', 'Описание'], sorted(rows_))
        except Exception as ex_:
            print(ex_)
            pass

        try:
            json_ = json.loads(open(self.pip_list_json).read())
            rows_ = []
            for r_ in json_:
                rows_.append([r_['name'], r_['version']])

            write_doc_table('reports/doc-python-packages.htm', ['Package', 'Version'], sorted(rows_))
        except Exception as ex_:
            print(ex_)
            pass

        spec = self.spec
        #!!! need to fix !!!
        abs_path_to_out_dir = os.path.abspath(self.out_dir)

        def cloc_for_files(clocname, filetemplate):
            cloc_csv = f'tmp/{clocname}.csv'
            if not os.path.exists(cloc_csv):
                if shutil.which('cloc'):
                    os.system(f'cloc {filetemplate} --csv  --timeout 3600  --report-file={cloc_csv} --3')
            if os.path.exists(cloc_csv):
                table_csv = []
                with open(cloc_csv, newline='') as csvfile:
                    csv_r = csv.reader(csvfile, delimiter=',', quotechar='|')
                    for row in list(csv_r)[1:]:
                        if 'Dockerfile' != row[1]:
                            row[-1] = int(float(row[-1]))
                            table_csv.append(row)

                table_csv[-1][-2], table_csv[-1][-1] = table_csv[-1][-1], table_csv[-1][-2]
                write_doc_table(f'tmp/{clocname}.htm', ['Файлов', 'Язык', 'Пустых', 'Комментариев', 'Строчек кода', 'Мощность языка', 'COCOMO строк'],
                                table_csv)

        cloc_for_files('our-cloc', f'./in/src/')
        cloc_for_files('libs-cloc', f'./{self.spec.libscon_dir}')
        
        ...        

    def clear_shell_files(self):
        os.chdir(self.curdir)
        re_ = re.compile(r'(\d\d-|ta-).*\.(bat)')
        for sh_ in Path(self.curdir).glob('*.*'):
            if re_.match(sh_.name):
                sh_.unlink()
        pass


    def process(self):
        '''
        Основная процедура генерации проекта,
        и возможно его выполнения, если соответствующие опции
        командной строки активированы.
        '''

        if self.args.folder_command:
            self.folder_command()
            return

        if self.args.git_sync:
            self.git_sync()
            return

        self.build_mode = False
        self.clear_shell_files()

        if 'util_commands' in self.spec:
            for util_name, command in self.spec['util_commands'].items():
                self.lines2bat(util_name, [command])

        for stage_ in self.stage_methods:
            stage_()

        self.build_mode = True
        for stage_ in self.stage_methods:
            stage_()

        pass
