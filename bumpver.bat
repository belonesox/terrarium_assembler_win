rem 
rmdir /S /Q dist
C:\ta-buildroot\python-x86-3.7.9\python.exe setup.py sdist bdist_wheel
C:\ta-buildroot\python-x86-3.7.9\python.exe -m twine upload dist/*
