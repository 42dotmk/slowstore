publish: build version_bump tag push
	poetry --build publish

build:
	poetry build

version_bump:
	poetry version patch

tag: 
	git tag -a `poetry version --short` -m "Release `poetry version --short`"

push:
	git push --tags


