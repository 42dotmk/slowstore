TEST_ARGS = "-v"

publish: build version_bump tag push
	poetry --build publish

build:
	poetry build

version_bump:
	poetry version patch

tag: 
	git tag -a `poetry version --short` -m "Release `poetry version --short`"

push:
	git commit -am "Release `poetry version --short`"
	git push
	git push --tags

test:
	poetry run pytest $(TEST_ARGS)


