rem 
rmdir /S /Q dist
C:\ta-buildroot\python-x86-3.7.9\python.exe setup.py sdist bdist_wheel
rem twine upload dist/*
