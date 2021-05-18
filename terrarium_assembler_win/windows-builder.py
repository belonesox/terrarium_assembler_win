# -*- coding: utf-8 -*-

"""
Idempotent installation and tuning of all windows stuff
"""

import sys
import os
import zipfile
import tempfile
import pathlib
import traceback
import shutil
import copy
# import parsesetup
import stat
import errno
import socket
import dataclasses as dc
from wheel_filename import parse_wheel_filename

# from collections import namedtuple
# from typing import NamedTuple


def errorRemoveReadonly(func, path, exc):
    excvalue = exc[1]
    if func in (os.rmdir, os.remove) and excvalue.errno == errno.EACCES:
        # change the file to be readable,writable,executable: 0777
        os.chmod(path, stat.S_IRWXU | stat.S_IRWXG | stat.S_IRWXO)  
        # retry
        func(path)
    pass

@dc.dataclass
class PythonPackageGit:
    git_url: str
    branch: str

    def get_dir(self):
        _, dir_ = os.path.split(self.git_url)
        if dir_.endswith('.git'):
            dir_ = dir_[:-4]
        return dir_    

@dc.dataclass
class VSBuild:
    '''
    Сборка проекта для VS студии (C/C++/.NET)    
    подкаталог/проект/конфигурация/платформ
    '''
    subdir: str
    project: str
    configuration: str = "Release"
    platforms: list = None

@dc.dataclass
class JSBuild:
    '''
    Сборка JS файлов в утилиты.
    Компиляция каждого JS-файла подкаталоге в проект
    '''
    subdir: str
    project: str


@dc.dataclass
class ProjectsGit:
    git_url: str
    branch: str
    builds: list

    def get_dir(self):
        _, dir_ = os.path.split(self.git_url)
        if dir_.endswith('.git'):
            dir_ = dir_[:-4]
        return dir_    


@dc.dataclass
class ISOTemplate:
    output_dir: str
    folders: dict



@dc.dataclass
class DownloadMe:
    url: str

    def artifact_name(self):
        _, dir_ = os.path.split(self.url)
        return dir_    

    def download_me_line(self, dir2download):
        scmd = 'wget --no-check-certificate -P %s -c %s ' % (dir2download, self.url)
        return scmd

    def install_me_lines(self, from_dir, install_path):
        return ''

@dc.dataclass
class DownloadMeNamed(DownloadMe):
    filename: str = None

    def download_me_line(self, dir2download):
        scmd = 'wget --no-check-certificate  -P %s -c %s ' % (dir2download, self.url)
        if self.filename:
            scmd = 'wget --no-check-certificate -O %s/%s -c %s ' % (dir2download, self.filename, self.url)

        return scmd


@dc.dataclass
class UtilityDistro(DownloadMe):
    url: str

    def install_me_lines(self, from_dir, install_path):
        aname = self.artifact_name()
        aname_, aext_ = os.path.splitext(aname)
        if aname.endswith('.msi'):
            scmds = ("""
copy %(from_dir)s\\%(aname)s %%TEMP%%\\%(aname)s    
msiexec.exe /I %%TEMP%%\\%(aname)s /QB-! INSTALLDIR="%(install_path)s" TargetDir="%(install_path)s"
            """ % vars()).split('\n')
        else:
            scmds = ("""
7z -y x  %(from_dir)s\\%(aname)s -o%(install_path)s
            """ % vars()).split('\n')
        
        scmds.append("set PATH=%s;%%PATH%%" % install_path)    
        return scmds

@dc.dataclass
class NamedUtilityDistro(UtilityDistro):
    version: str

@dc.dataclass
class PathUtilityDistro(UtilityDistro):
    path: str

@dc.dataclass
class ExeUtilityDistro(NamedUtilityDistro):
    exeflags: str

    def install_me_lines(self, from_dir, install_path):
        aname = self.artifact_name()
        exeflags = self.exeflags % install_path
        scmds = ("""
%(from_dir)s\\%(aname)s %(exeflags)s
        """ % vars()).split('\n')
        scmds.append("set PATH=%s;%%PATH%%" % install_path)    
        return scmds


@dc.dataclass
class PythonDistro:
    version: str
    url: str

    def artifact_name(self):
        _, dir_ = os.path.split(self.url)
        return dir_    

    def python_dir(self):
        return "python-" + self.version

@dc.dataclass
class MSVCCompiler:
    version: str
    url: str
    components: list

    def get_add_line(self):
        res = " ".join(["--add " + comp for comp in self.components])
        return res




def mkdir_p(path):
    pathlib.Path(path).mkdir(parents=True, exist_ok=True)    


@dc.dataclass
class NuitkaFlags:
    std_flags: str
    force_packages: list
    force_modules: list
    block_packages: list

    def get_flags(self, out_dir):
        flags = ("""
            %s --output-dir="%s"    
        """ % (self.std_flags, out_dir)).strip().split("\n")        
        for it_ in self.force_packages:
            flags.append('--include-package=' + it_)
        for it_ in self.force_modules:
            flags.append('--include-module=' + it_)
        for it_ in self.block_packages:
            flags.append('--recurse-not-to=' + it_)

        return " ".join(flags)


@dc.dataclass
class BuildProject:
    input_py: str
    nuitka_flags: NuitkaFlags 
    copy_dll_from_folders: list = None
    copy_folders: list = None
    copy_src_files: list = None
    copy_and_rename_files: list = None

    def __post_init__(self):
        self.name = os.path.splitext(self.input_py.split("\\")[-1])[0]
        pass

def n(s):
    return s.replace('/','\\')

@dc.dataclass
class DistroPackage:
    build_projects: list
    output_template: ISOTemplate

@dc.dataclass
class DMDistroGenerator:
    msvc: MSVCCompiler
    tess: NamedUtilityDistro
    imagick: NamedUtilityDistro
    python: PythonDistro
    ppackages_git: list
    projects_git: list
    utilities: list
    distro_package: DistroPackage

    def __post_init__(self):
        self.curdir = os.getcwd()
        self.output_dir = 'distro'
        self.install_requires = ["pip"]
        self.in_dir =  'in'
        # self.out_dir = R'..\out'
        self.src_dir = os.path.join(self.in_dir, 'src')
        self.bin_dir = os.path.join(self.in_dir, 'bin')
        self.bin_dirw = self.bin_dir.replace('/', '\\')
        self.extwheel_dir = os.path.join(self.bin_dir, "extwheel")
        self.ourwheel_dir = os.path.join(self.bin_dir, "ourwheel")
        mkdir_p(os.path.join(self.output_dir, self.extwheel_dir))
        mkdir_p(os.path.join(self.output_dir, self.ourwheel_dir))

        # self.pip_bootstrap = "pip-20.1.1-py2.py3-none-any.whl"
        mkdir_p(os.path.join(self.output_dir, self.src_dir))
        mkdir_p(os.path.join(self.output_dir, self.bin_dir))
        self.buildroot =  'c:\\docmarking-buildroot'
        self.nuitkaroot = os.path.join(self.buildroot, 'nuitka-builds')
        self.vsbuildroot = os.path.join(self.buildroot, 'vsbuild')
        mkdir_p(self.nuitkaroot)
        self.pythondir = n(os.path.join(self.buildroot, self.python.python_dir()))
        self.tessdir = n(os.path.join(self.buildroot,  self.tess.version))
        self.imagickdir = n(os.path.join(self.buildroot, self.imagick.version))

        # if not os.path.exists(os.path.join(self.output_dir, "smoketests")):
        #     shutil.copytree("smoketests", os.path.join(self.output_dir, "smoketests"))
        pass

    def lines2bat(self, name, lines):
        os.chdir(self.curdir)
        os.chdir(self.output_dir)
        with open(os.path.join(name+".bat"), 'w', encoding="utf-8") as lf:
            lf.write("rem Generated %s \n" % name)
            lf.write("\n".join(lines))
            # for l in lines:
            #     if "http" in l or "del " in l:
            #         lf.write(l)
            #     else:
            #         lf.write(l.replace('/','\\'))
            #     lf.write('\n')
            # lf.write("\n".join([l.replace('/','\\') for l in lines if "http" not in l]))
        pass    

    # def generate_download_and_vendor(self):
    #     os.chdir(self.curdir)
    #     os.chdir(self.output_dir)
    #     lines = []
    #     scmd = f'wget -P {self.bin_dir}/wsdk8  -c "http://download.microsoft.com/download/B/0/C/B0C80BA3-8AD6-4958-810B-6882485230B5/standalonesdk/sdksetup.exe"'
    #     lines.append(scmd)
    #     self.lines2bat("11-download-and-install-msvc2013", lines)
    #     pass    

    def generate_download(self):
        os.chdir(self.curdir)
        os.chdir(self.output_dir)
        lines = []
        scmd = f'wget --no-check-certificate -P {self.bin_dir} -c {self.python.url} '
        lines.append(scmd)

        scmd = f'wget --no-check-certificate -P {self.bin_dir}/wdk10  -c "https://download.microsoft.com/download/1/a/7/1a730121-7aa7-46f7-8978-7db729aa413d/wdk/wdksetup.exe" '
        lines.append(scmd)


        scmd = f'wget --no-check-certificate  -c "https://getbox.ispras.ru/index.php/s/C9MiOGPwq6CZXDS/download" -O "%TEMP%/vs2013_and_wdk81.zip"'
        lines.append(scmd)


        # scmd = f'wget -P {self.bin_dir}/wdk8  -c "http://download.microsoft.com/download/2/4/C/24CA7FB3-FF2E-4DB5-BA52-62A4399A4601/wdk/wdksetup.exe" '
        # lines.append(scmd)

        scmd = f'wget -P {self.bin_dir}/wsdk8  -c "http://download.microsoft.com/download/B/0/C/B0C80BA3-8AD6-4958-810B-6882485230B5/standalonesdk/sdksetup.exe"'
        lines.append(scmd)
 
        scmd = f'wget --no-check-certificate -c "https://nextcloud.ispras.ru/s/xak8xaSoC8jSbR3/download" -O {self.bin_dir}/vcredist_x86_vs2019.exe  --no-check-certificate'
        lines.append(scmd)
        
        #Эта сволочь скачивает асинхронно, без подтверждения, лучше ее запустить пораньше, до скачивания MSVC
        #scmd = fR'{self.bin_dirw}\wdk10\wdksetup.exe /layout {self.bin_dir}\wdk10 /q ' 
        #lines.append(scmd)

        #scmd = f'7z x -y %TEMP%/vs2013_and_wdk81.zip -o {self.bin_dir}'
        #lines.append(scmd)
        # scmd = fR'{self.bin_dirw}\wdk8\wdksetup.exe /layout {self.bin_dir}\wdk8 /q ' 
        # lines.append(scmd)

        #scmd = fR'{self.bin_dirw}\wsdk8\sdksetup.exe /layout {self.bin_dir}\wsdk8 /q ' 
        #lines.append(scmd)


        scmd = 'wget --no-check-certificate -P %s -c %s ' % (self.bin_dir, self.msvc.url)
        lines.append(scmd)

        scmd = 'wget --no-check-certificate  -P %s -c %s ' % (self.bin_dir, self.tess.url)
        lines.append(scmd)

        scmd = 'wget --no-check-certificate  -P %s -c %s ' % (self.bin_dir, self.imagick.url)
        lines.append(scmd)

        for ut_ in self.utilities:
            lines.append(ut_.download_me_line(self.bin_dir))
      
        self.lines2bat("01-download", lines)
        pass    
        
    def generate_rename(self):
        os.chdir(self.curdir)
        os.chdir(self.output_dir)
        lines = []
        scmd = f'rename out\iso\dm-embed-pipeline\screenmark.exe SnSm.exe '
        lines.append(scmd)

        scmd = f'rename out\iso_sn\dm-embed-pipeline\screenmark.exe SnSm.exe '
        lines.append(scmd)
      
        self.lines2bat("60-rename-screenmark", lines)
        pass    

    def generate_install(self):
        os.chdir(self.curdir)
        os.chdir(self.output_dir)
        lines = []

        scmd = '%s\\vs_BuildTools.exe --layout %s/%s --lang en-US %s ' % (
                        n(self.bin_dir), n(self.bin_dir), self.msvc.version, self.msvc.get_add_line())
        lines.append(scmd)

        scmd = '%s\\%s\\vs_BuildTools.exe -p  %s ' % (
                        n(self.bin_dir), self.msvc.version, self.msvc.get_add_line())
        lines.append(scmd)
        
        # scmd = R'%s\wdk\wdksetup.exe /q ' % (self.bin_dir.replace('/', '\\') )
        # lines.append(scmd)

        scmd = fR'{self.bin_dirw}\wdk10\wdksetup.exe /q ' 
        lines.append(scmd)

        scmd = fR'{self.bin_dirw}\wdk8.1\wdksetup.exe /q ' 
        lines.append(scmd)

        scmd = fR'{self.bin_dirw}\msvc2013\vs_ultimate.exe /NoRestart /Passive  ' 
        lines.append(scmd)

        scmd = fR'{self.bin_dirw}\wsdk8\sdksetup.exe /q ' 
        lines.append(scmd)

        self.lines2bat("10-install-msvc", lines)

        # do not append installs
        lines = []

        scmd = '%s\\%s /passive TargetDir=%s ' % (n(self.bin_dir), self.python.artifact_name(), self.pythondir)
        lines.append(scmd)

        scmd = "%s\\python -m pip install wheel cython clcache " % (self.pythondir)
        lines.append(scmd)

        for ut_ in self.utilities + [self.tess, self.imagick]:
            install_dir = self.pythondir
            try:
                install_dir = os.path.join(self.buildroot, ut_.version)
            except:
                pass    
            try:
                install_dir = ut_.path
            except:
                pass    
            # if 'magick' in ut_.url:
            #     wtf = 1
            lines +=  ut_.install_me_lines(self.bin_dir, install_dir)

        our_wheels = [n(os.path.join(self.ourwheel_dir, whl)) for whl in os.listdir(self.ourwheel_dir) if whl.endswith('.whl')]
        our_wheels_set = set([parse_wheel_filename(whl.replace('\\', '/')).project for whl in our_wheels])

        ext_wheels = [n(os.path.join(self.extwheel_dir, whl)) for whl in os.listdir(self.extwheel_dir) if whl.endswith('.whl') and parse_wheel_filename(whl).project not in our_wheels_set]
        ext_src = [n(os.path.join(self.extwheel_dir, whl)) for whl in os.listdir(self.extwheel_dir) if whl.endswith('tar.gz') or whl.endswith('tar.bz2')]

        for wheel in ext_wheels + our_wheels:
            scmd = '%s\\python -m pip install --upgrade   %s ' % (self.pythondir, wheel) # --force-reinstall
            lines.append(scmd)

        self.lines2bat("11-install", lines)
        pass    


    def generate_builds_vsprojects(self):
        os.chdir(self.curdir)
        os.chdir(self.output_dir)

        bat_names = []
        for proj in self.projects_git:
            src_dir = os.path.join(self.src_dir, proj.get_dir())
            for build in proj.builds:
                projectname_ = build.project
                lines = []
                if isinstance(build, VSBuild):
                    projectfile_ = build.project + '.sln'
                    if '.' in build.project:
                        projectname_ = os.path.splitext(build.project)[0]
                        projectfile_ = build.project

                    lines.append(R"""
call "C:\Program Files (x86)\Microsoft Visual Studio\2019\BuildTools\Common7\Tools\VsDevCmd.bat"
    """ % vars(self))
                    if build.platforms:
                        for platform_ in build.platforms:
                            lines.append(fR"""     
    msbuild  /p:Configuration="{build.configuration}" /p:Platform="{platform_}" {src_dir}\{build.subdir}\{projectfile_}

    msbuild  /p:OutputPath="{self.vsbuildroot}\{projectname_}\{platform_}" /p:OutDir="{self.vsbuildroot}\{projectname_}\{platform_}\\" /p:Configuration="{build.configuration}" /p:Platform="{platform_}" {src_dir}\{build.subdir}\{projectfile_}
        """)
                    else:
                        lines.append(fR"""     
    msbuild  /p:Configuration="{build.configuration}"  {src_dir}\{build.subdir}\{projectfile_}

    msbuild  /p:OutputPath="{self.vsbuildroot}\{projectname_}" /p:OutDir="{self.vsbuildroot}\{projectname_}" /p:Configuration="{build.configuration}"  {src_dir}\{build.subdir}\{projectfile_}
    """)
                elif isinstance(build, JSBuild):
                    outdir_ = os.path.join(self.vsbuildroot, build.project, build.subdir)
                    lines.append(fR"mkdir {outdir_}")
                    for file_ in os.listdir(os.path.join(src_dir, build.subdir)):
                        if file_.endswith('.js'):
                            infile = os.path.join(src_dir, build.subdir, file_)
                            outfile = os.path.join(outdir_, os.path.splitext(file_)[0] + '.exe')
                            lines.append(fR"""     
C:\Windows\Microsoft.NET\Framework\v4.0.30319\jsc /out:{outfile}  {infile}                            
        """)

                bat_name = "38-build-%s" % projectname_
                bat_names.append(bat_name)
                self.lines2bat(bat_name, lines)

        lines = ['call %s.bat ' % bn for bn in bat_names]
        self.lines2bat('39-build-all-vsprojects', lines)
        pass    


    def generate_builds_projects(self):
        os.chdir(self.curdir)
        os.chdir(self.output_dir)

        bat_names = []
        for proj in self.distro_package.build_projects:
            lines = []
            #некрасиво, но пусть пока так.
            lines.append("""
set NUITKA_CLCACHE_BINARY=%(pythondir)s\\Scripts\\clcache.exe
set CLCACHE_DIR=%%TEMP%%\\CLCACHE
set CXXFLAGS=/D_USING_V110_SDK71_
rem set LDFLAGS=/SUBSYSTEM:CONSOLE,5.01
set PYTHONHOME=%(pythondir)s
""" % vars(self))
            nflags = proj.nuitka_flags.get_flags(self.nuitkaroot)
            lines.append(R"""
%s\python.exe -m nuitka %s in\src\%s 
""" % (self.pythondir, nflags, proj.input_py))

            if proj.copy_dll_from_folders:
                for fld_ in proj.copy_dll_from_folders:
                    if not os.path.isabs(fld_):
                        fld_ = os.path.join(self.buildroot, fld_)
                    lines.append(R"""
echo n | copy /-y "%s\*.dll" %s\%s.dist\ 
""" % (fld_, self.nuitkaroot, proj.name))


            if proj.copy_src_files:
                for fld_ in proj.copy_src_files:
                    if not os.path.isabs(fld_):
                        fld_ = os.path.join(self.src_dir, fld_)
                    lines.append(R"""
echo n | copy /-y "%s" %s\%s.dist\ 
""" % (fld_, self.nuitkaroot, proj.name))

            if proj.copy_and_rename_files:
                for from_, to_ in proj.copy_and_rename_files:
                    # if not os.path.isabs(fld_):
                    #     fld_ = os.path.join(self.src_dir, fld_)
                    lines.append(R"""
echo n | copy /-y "%s" %s\%s.dist\%s 
""" % (from_, self.nuitkaroot, proj.name, to_))

            if proj.copy_folders:
                for fld_, to_ in proj.copy_folders:
                    if not os.path.isabs(fld_):
                        fld_ = os.path.join(self.buildroot, fld_)
                    lines.append(R"""
echo n | xcopy /I /S /Y /D  %s %s\%s.dist\%s 
""" % (fld_, self.nuitkaroot, proj.name, to_))

            bat_name = "30-build-%s" % proj.name
            bat_names.append(bat_name)
            self.lines2bat(bat_name, lines)


        lines = ['call %s.bat ' % bn for bn in bat_names]
        self.lines2bat('40-build-all-projects', lines)
        pass    

        #lines = []
        #lines.append('rmdir /S /Q %s ' % n(self.distro_package.output_dir))
        #lines.append('mkdir %s ' % n(self.distro_package.output_dir))
        #for proj in self.distro_package.build_projects:
        #    lines.append(R"""    
#echo n | xcopy /I /S /Y  %s\%s.dist\* %s\
#""" % (self.nuitkaroot, proj.name, self.distro_package.output_dir))

    def generate_merge_projects(self):
        lines = []
        template_ = self.distro_package.output_template
        out_dir = template_.output_dir
        lines.append(fR'rmdir /S /Q "{out_dir}" ')
        lines.append(fR'mkdir "{out_dir}" ')

        buildroot = self.buildroot
        srcdir  = self.src_dir
        bindir  = self.bin_dir
        for folder, sources_ in template_.folders.items():
            if isinstance(sources_, str):
                sources_ = [s.strip() for s in sources_.strip().split("\n")]
            dst_folder = os.path.join(out_dir, folder)
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
        self.lines2bat('50-merge', lines)
        pass    

    
    def generate_tools_install(self):
        os.chdir(self.curdir)
        os.chdir(self.output_dir)
        scmd = R"""
@"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -InputFormat None -ExecutionPolicy Bypass -Command " [System.Net.ServicePointManager]::SecurityProtocol = 3072; iex ((New-Object System.Net.WebClient).DownloadString('https://chocolatey.org/install.ps1'))" && SET "PATH=%PATH%;%ALLUSERSPROFILE%\chocolatey\bin"
choco install -y far procmon wget 
""" 
        self.lines2bat("99-install-tools", [scmd])

        scmd = R"""
call 11-install.bat
call 40-build-all-projects.bat
""" 
        self.lines2bat("80-install-and-build", [scmd])


        lines = []
        sandboxfile = 'puresandbox.wsb'
        lines.append("del /q %(sandboxfile)s" % vars())
        lines.append(R"mkdir %cd%\out")
        def l2bat(line):
            line_ = 'echo ' + line.replace("<", "^<").replace(">", "^>") + ' >> ' + sandboxfile 
            lines.append(line_)

        l2bat(R"<Configuration><MappedFolders>")
        l2bat(R"<MappedFolder><HostFolder>%cd%</HostFolder>")
        l2bat(R"<SandboxFolder>C:\Users\WDAGUtilityAccount\Desktop\distro</SandboxFolder>")
        l2bat(R"<ReadOnly>true</ReadOnly></MappedFolder>")
        l2bat(R"<MappedFolder><HostFolder>%cd%\out</HostFolder>")
        l2bat(R"<SandboxFolder>C:\Users\WDAGUtilityAccount\Desktop\out</SandboxFolder>")
        l2bat(R"<ReadOnly>false</ReadOnly></MappedFolder>")
        l2bat(R"</MappedFolders>")
        l2bat(R"<LogonCommand>")
        l2bat(R"<Command>C:\Users\WDAGUtilityAccount\Desktop\distro\99-install-tools.bat</Command>")
        l2bat(R"</LogonCommand>")
        l2bat(R"</Configuration>")
        self.lines2bat("90-generate-sanboxes", lines)
        pass    


    def download_wheels(self):
        os.chdir(self.curdir)
        os.chdir(self.output_dir)
        lines = []

        scmd = '%s\\%s /passive TargetDir=%s ' % (n(self.bin_dir), self.python.artifact_name(), self.pythondir)
        lines.append(scmd)

        scmd = "%s\\python -m pip install wheel cython clcache " % (self.pythondir)
        lines.append(scmd)                

        scmd = fR"{self.pythondir}\python -m pip wheel pyuv -w  {self.bin_dir}\extwheel " 
        lines.append(scmd)  
        for pack in self.ppackages_git:
            pdir = pack.get_dir()
            # scmd = "%s\\python -m pip download %s\\%s --dest %s\\extwheel " % (
            #     self.pythondir, self.src_dir, pdir, self.bin_dir)
            scmd = fR"{self.pythondir}\python -m pip wheel {self.src_dir}\{pdir} -w  {self.bin_dir}\extwheel " 
            lines.append(scmd)  
            pass
        scmd = "%s\\python -m pip download py2exe wheel clcache --dest %s\\extwheel " % (
            self.pythondir, self.bin_dir)
        lines.append(scmd)                
        self.lines2bat("02-download-wheels", lines)
        pass    

    def checkout_sources(self):
        os.chdir(self.curdir)
        os.chdir(self.output_dir)
        os.chdir(self.src_dir)
        for pack in self.ppackages_git + self.projects_git:
            pdir = pack.get_dir()
            pdir_old = pdir + '.old'
            if os.path.exists(pdir_old):
                os.system('rmdir /S /Q "{}"'.format(pdir_old))
                shutil.rmtree(pdir_old, ignore_errors=False, onerror=errorRemoveReadonly)
            if os.path.exists(pdir):
                os.rename(pdir, pdir_old)
            scmd = "git clone --single-branch --branch %(branch)s %(git_url)s " % vars(pack)
            os.system(scmd)

    def build_wheels(self):
        os.chdir(self.curdir)
        bindir_ = os.path.abspath(self.bin_dir)
        lines = []
        lines.append(R"del /Q %s\*.*" % n(self.ourwheel_dir))
        for pack in self.ppackages_git:
            os.chdir(self.curdir)
            os.chdir(self.output_dir)
            pdir = pack.get_dir()
            os.chdir(self.src_dir)
            os.chdir(pdir)
            scmd = "pushd %s\\%s" % (n(self.src_dir), pdir)
            lines.append(scmd)
            scmd = "%(pythondir)s\\python setup.py install " % vars(self)
            lines.append(scmd)
            scmd = "%(pythondir)s\\python setup.py bdist_wheel -d ..\\..\\..\\%(ourwheel_dir)s " % vars(self)
            lines.append(scmd)
            lines.append('popd')
            pass
        batfile = "04-build-wheels"
        self.lines2bat(batfile, lines)
        os.chdir(self.curdir)
        os.chdir(self.output_dir)
        os.system(batfile+'.bat')
        pass

        #     if 0:
        #         setup_file = os.path.join(pack.get_dir(), 'setup.py')
        #         curdir_ = os.getcwd()
        #         setup_args = parsesetup.parse_setup(setup_file, trusted=True)
        #         os.chdir(curdir_)
        #         if "install_requires" in setup_args:
        #             self.install_requires += setup_args["install_requires"]
        #     pass
        # os.chdir(curdir__)
        # print(self.install_requires)
        # bat_lines = ["rem"]
        # for req in self.install_requires:
        #     bat_lines.append("")
        # pass

def main():
    imagick = ExeUtilityDistro('https://imagemagick.org/download/binaries/ImageMagick-7.0.10-28-Q16-x86-dll.exe', 'imagick-7.0.10', ' /DIR=%s /SP /VERYSILENT /NOCANCEL /SUPPRESSMSGBOXES')
    tess = NamedUtilityDistro("https://digi.bib.uni-mannheim.de/tesseract/tesseract-ocr-w32-setup-v5.0.0-alpha.20200328.exe", "tesseract-5.0.0-x86")
    # python = PythonDistro('x86-3.8.4', 'https://www.python.org/ftp/python/3.8.4/python-3.8.4.exe')
    python = PythonDistro('x86-3.7.8', 'https://www.python.org/ftp/python/3.7.8/python-3.7.8.exe')

    print(python.python_dir())

    nuitka_flags_console = NuitkaFlags(
        # std_flags = ' --show-progress --show-scons --standalone --graph  --plugin-enable=numpy=scipy '
        std_flags=' --show-progress --show-scons --standalone --graph  --plugin-enable=numpy --include-scipy ', # 
        force_packages='procedure wand scipy.special tesserocr'.split(),
        force_modules='procedure.algorithm.xwn_watermarking procedure.algorithm.dm_shift procedure.algorithm.dm_strikethrough PIL._imaging scipy.special.cython_special skimage.feature._orb_descriptor_positions '.split(),
        block_packages='astropy sympy dask ipywidgets ipython_genutils ipykernel IPython pexpect nbformat numpydoc matplotlib pandas pytest nose'.split()
    )                 

    nuitka_flags_no_console = copy.deepcopy(nuitka_flags_console)
    nuitka_flags_no_console.std_flags += '  --windows-disable-console ' 

    dg = DMDistroGenerator(
        msvc=MSVCCompiler("msvc2019", 
            "https://download.visualstudio.microsoft.com/download/pr/408ac6e1-e3ac-4f0a-b327-8e57a845e376/2f6c9392fe8038a710889e2639862c9ccc4e857fb6791ddd0aea183033eb3aab/vs_BuildTools.exe",
            ['Microsoft.VisualStudio.Workload.MSBuildTools',
                'Microsoft.VisualStudio.Workload.VCTools',
                'Microsoft.VisualStudio.Component.VC.Tools.x86.x64',
                'Microsoft.VisualStudio.Component.VC.v141.x86.x64',
                'Microsoft.VisualStudio.Component.VC.140',
                'Microsoft.Net.Component.4.TargetingPack',
                # 'Microsoft.Net.Component.4.8.SDK',
                # 'Microsoft.Net.ComponentGroup.4.8.DeveloperTools',
                'Microsoft.Component.MSBuild',
                # 'Microsoft.Net.Component.4.6.1.TargetingPack',
                # 'Microsoft.VisualStudio.Component.NuGet.BuildTools',
                # 'Microsoft.VisualStudio.Component.Roslyn.Compiler',
                # 'Microsoft.Component.ClickOnce.MSBuild',
                'Microsoft.VisualStudio.Component.Windows10SDK.18362',
                #'Microsoft.VisualStudio.Component.Windows10SDK.19041'
                ]
            ),
        python=python,
        ppackages_git=[
                PythonPackageGit('git@gitlab.ispras.ru:watermarking/dmconfig.git', 'nuitka'),
                PythonPackageGit('git@gitlab.ispras.ru:watermarking/dm-marker-generator.git', 'nuitka'),
                PythonPackageGit('git@gitlab.ispras.ru:watermarking/dm-algorithm.git', 'nuitka'),
                PythonPackageGit('git@gitlab.ispras.ru:watermarking/dm-pipeline.git', 'nuitka'),
                PythonPackageGit('git@gitlab.ispras.ru:watermarking/dm-psi.git', 'nuitka'),
                PythonPackageGit('git@gitlab.ispras.ru:watermarking/xwn_watermarking.git', 'nuitka'),
                PythonPackageGit('git@gitlab.ispras.ru:watermarking/dm-gslh18.git', 'nuitka'),
                PythonPackageGit('git@gitlab.ispras.ru:watermarking/dm-logger.git', 'nuitka'),
                PythonPackageGit('git@gitlab.ispras.ru:watermarking/screenmark-win.git', 'nuitka'),
                PythonPackageGit('git@gitlab.ispras.ru:watermarking/dmspectator.git', 'nuitka'),
                PythonPackageGit('https://github.com/belonesox/pyspectator.git', 'master'),
                # PythonPackageGit('git@gitlab.ispras.ru:watermarking/lum_watermarking.git', 'nuitka'),
                # PythonPackageGit('https://github.com/belonesox/Nuitka', 'hack-0.6.7-for-skimage'),
                #PythonPackageGit('https://github.com/Nuitka/Nuitka', 'cb68905ccd81b2dbfb79834c588c427449fde53e')
                PythonPackageGit('https://github.com/belonesox/Nuitka', 'hack-master-for-networkx-and-skimage'),
                PythonPackageGit('https://github.com/belonesox/networkx', 'hack-for-nuitka'),
            ],
        projects_git = [
                ProjectsGit('git@gitlab.ispras.ru:watermarking/dm_logger_c.git', 'master',[
                    VSBuild('', 'dm_logger.vcxproj', configuration='Release', platforms=['Win32']),
                ]),
                ProjectsGit('git@gitlab.ispras.ru:watermarking/xpsdriverrollback.git', 'master',[
                    VSBuild('', 'XPSDriverRollback.sln', configuration='Release', platforms=['Any CPU']),
                ]),
                ProjectsGit('git@gitlab.ispras.ru:watermarking/dm_service.git', 'master',[
                    VSBuild('', 'dm_service.vcxproj', configuration='Release', platforms=['Win32']),
                ]),
                ProjectsGit('git@gitlab.ispras.ru:watermarking/DMPrinterWatermarkService.git', 'master',[
                    VSBuild('DMPrinterWatermarkService', 'DMPrinterWatermarkService.sln', configuration='Release', platforms=['Any CPU']),
                ]),
                ProjectsGit('git@gitlab.ispras.ru:watermarking/PrinterPortInstaller.git', 'master',[
                    VSBuild('PrinterPortInstaller', 'PrinterPortInstaller.sln', configuration='Release', platforms=['Any CPU']),
                ]),
                ProjectsGit('git@gitlab.ispras.ru:watermarking/XpsUmdfDriver.git', 'master',[
                    VSBuild('XpsUmdfDriver', 'XpsUmdfDriver.sln', configuration='Win8.1 Debug', platforms=['Win32', 'x64']),
                ]),
                ProjectsGit('git@gitlab.ispras.ru:watermarking/SecretNetDll.git', 'master',[
                    VSBuild('SecretNetDll', 'WatermarkModule.sln', configuration='Win8.1 Release', platforms=['Win32', 'x64'])
                ]),
                ProjectsGit('git@gitlab.ispras.ru:watermarking/dmprinter_win_install.git', 'master',[
                    JSBuild('', 'win-install-utils'),
                ]),
                ProjectsGit('git@gitlab.ispras.ru:watermarking/dm-windows-configs.git', 'master', []),        
            ],
        imagick=imagick,
        tess=tess, 
        utilities=[
            NamedUtilityDistro("https://www.7-zip.org/a/7z1604.msi", "7z"),
            DownloadMe("https://download.microsoft.com/download/2/0/E/20E90413-712F-438C-988E-FDAA79A8AC3D/dotnetfx35.exe"),
            DownloadMe("https://download.microsoft.com/download/9/5/A/95A9616B-7A37-4AF6-BC36-D6EA96C8DAAE/dotNetFx40_Full_x86_x64.exe"),
            DownloadMeNamed("https://github.com/simonflueckiger/tesserocr-windows_build/releases/download/tesserocr-v2.4.0-tesseract-4.0.0/tesserocr-2.4.0-cp37-cp37m-win32.whl", "extwheel/tesserocr-2.4.0-cp37-cp37m-win32.whl"),
            DownloadMeNamed("https://getbox.ispras.ru/index.php/s/48vMtihTZkx3PK6/download", "wic_x64.exe"),
            DownloadMeNamed("https://getbox.ispras.ru/index.php/s/cOE5omlNnVg5EXV/download", "wic_x86.exe"),
            DownloadMeNamed("https://download.microsoft.com/download/C/6/D/C6D0FD4E-9E53-4897-9B91-836EBA2AACD3/vcredist_x86.exe", "vcredist_x86_vs2010.exe"),
            PathUtilityDistro("https://www.dependencywalker.com/depends22_x86.zip", R"%USERPROFILE%\AppData\Local\Nuitka\Nuitka\x86"),
            # UtilityDistro("https://download.microsoft.com/download/1/4/9/14936FE9-4D16-4019-A093-5E00182609EB/Windows6.1-KB2670838-x86.msu"),
        ],
        distro_package = DistroPackage(
            build_projects = [
                BuildProject(
                    input_py = R"dm-pipeline\dm-embed-pipeline.py",
                    nuitka_flags = nuitka_flags_console,
                    copy_dll_from_folders = [
                        imagick.version,    
                        os.path.join(python.python_dir(), R"Lib\site-packages\scipy\.libs"),
                        os.path.join(python.python_dir(), R"Lib\site-packages\numpy\.libs"),
                        R"C:\Program Files (x86)\Windows Kits\10\Redist\ucrt\DLLs\x86"                        
                    ],
                    copy_folders = [
                        (os.path.join(python.python_dir(), R"Lib\site-packages\skimage\feature"), R"skimage\feature"),
                        (tess.version, 'tesseract')
                    ]
                ),
                BuildProject(
                    input_py = R"screenmark-win\screenmark.py",
                    #nuitka_flags = nuitka_flags_no_console,
                    nuitka_flags = nuitka_flags_console,
                    copy_dll_from_folders = [
                        #os.path.join(python.python_dir(), R"Lib\site-packages\shapely\DLLs"),
                        os.path.join(python.python_dir(), R"Lib\site-packages\numpy\.libs"),
                        os.path.join(python.python_dir(), R"Lib\site-packages\scipy\.libs"),
                        R"C:\Program Files (x86)\Windows Kits\10\Redist\ucrt\DLLs\x86"                        
                    ],
                    copy_and_rename_files = [
                        (R'C:\Windows\SysWOW64\downlevel\api-ms-win-core-shlwapi-legacy-l1-1-0.dll',
                         R'api-ms-win-downlevel-shlwapi-l1-1-0.dll')
                    ],
                    copy_src_files = [
                        R'screenmark-win\config\gslh18_mark.json'
                    ]
                ),
                BuildProject(
                    input_py = R"dmspectator\dm-spectator.py",
                    #nuitka_flags = nuitka_flags_no_console,
                    nuitka_flags = nuitka_flags_console,
                    copy_dll_from_folders = [
                        #os.path.join(python.python_dir(), R"Lib\site-packages\shapely\DLLs"),
                        # os.path.join(python.python_dir(), R"Lib\site-packages\numpy\.libs"),
                        # os.path.join(python.python_dir(), R"Lib\site-packages\scipy\.libs"),
                        R"C:\Program Files (x86)\Windows Kits\10\Redist\ucrt\DLLs\x86"                        
                    ],
                    copy_and_rename_files = [
                        (R'C:\Windows\SysWOW64\downlevel\api-ms-win-core-shlwapi-legacy-l1-1-0.dll',
                         R'api-ms-win-downlevel-shlwapi-l1-1-0.dll')
                    ],
                    # copy_src_files = [
                    #     R'screenmark-win\config\gslh18_mark.json'
                    # ]
                ),
            ],    
            # output_dir=R'..\out\dm32',
            output_template = ISOTemplate(
                R'out\iso',
                {
                    "distrib": R"""
                        {bindir}\dotnetfx35.exe
                        {bindir}\dotNetFx40_Full_x86_x64.exe
                        {bindir}\vcredist_x86_vs2010.exe
                        {bindir}\vcredist_x86_vs2019.exe
                        {bindir}\wic_x64.exe
                        {bindir}\wic_x86.exe
                    """,
                    "dm-embed-pipeline": R"""
                        {buildroot}\vsbuild\dm_service\Win32\dm_logger.dll
                        {buildroot}\vsbuild\dm_service\Win32\dm_service.exe
                        {buildroot}\vsbuild\DMPrinterWatermarkService\DMPrinterWatermarkService.exe
                        {buildroot}\nuitka-builds\dm-embed-pipeline.dist
                        {buildroot}\nuitka-builds\screenmark.dist
                        {buildroot}\nuitka-builds\dm-spectator.dist
                        {srcdir}\dm-windows-configs
                    """,
                    R"DMPrinterDriver\x32": R"{buildroot}\vsbuild\XpsUmdfDriver\Win32\XpsUmdfDriver Package",
                    R"DMPrinterDriver\x64": R'{buildroot}\vsbuild\XpsUmdfDriver\x64\XpsUmdfDriver Package',                    
                    "misc": R"""    
                        {buildroot}\vsbuild\win-install-utils\DisableTestmodeReboot.exe
                        {buildroot}\vsbuild\win-install-utils\InstallDMPrinter.exe
                        {buildroot}\vsbuild\win-install-utils\InstallScreenMarking.exe
                        {buildroot}\vsbuild\win-install-utils\UninstallDMPrinter.exe
                        {buildroot}\vsbuild\win-install-utils\UninstallScreenMarking.exe                        
                        {buildroot}\vsbuild\PrinterPortInstaller\PrinterPortInstaller.exe
                    """,
                    R"misc\XPSDriverRollback": R"{buildroot}\vsbuild\XPSDriverRollback\Any CPU\XPSDriverRollback.exe",
                    R"misc\XPSDriverRollback\x32": R"{srcdir}\XPSDriverRollback\x32",
                    R"misc\XPSDriverRollback\x64": R'{srcdir}\XPSDriverRollback\x64',
                    "": R"""    
                        {buildroot}\vsbuild\win-install-utils\CopyAndRunInstallDMMarking.exe
                        {buildroot}\vsbuild\win-install-utils\RunUninstallDMMarking.exe
                    """,
                } 
            )            
        )
    )

    # if '10.11.20' not in socket.gethostbyname(socket.gethostname()):
    dg.checkout_sources()            #  <------
    
    # dg.generate_install()
    #return
    #'''
    
    dg.generate_builds_projects()
    dg.generate_builds_vsprojects()
    dg.generate_merge_projects()    
    dg.generate_download()
    
    dg.download_wheels()            #  <------
    dg.build_wheels()               #  <------
    
    dg.generate_install()

    #''' 
    # dg.generate_tools_install()
    # dg.checkout_sources()
    
    #dg.generate_merge_projects()

    dg.generate_rename()

    # dg.generate_builds_projects()
    # dg.generate_builds_vsprojects()
    pass

if __name__ == '__main__':
    main()

#git@gitlab.ispras.ru:watermarking/windowsprinterwm.git