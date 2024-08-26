build:
	poetry build

version_bump: 
	poetry version patch

tag: 
	git tag -a `poetry version --short` -m "Release `poetry version --short`"

push:
	git push --tags

publish: build, version_bump, tag
	poetry publish --build --username __token__ --password ${PYPI_TOKEN}

