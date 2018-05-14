#!/usr/bin/env bash

# this scripts needs to be called from the Project's root dir


# generate .rst files from module code & docstrings
# any pathnames given at the end are paths to be excluded ignored during generation.
# ref: http://www.sphinx-doc.org/en/master/man/sphinx-apidoc.html
sphinx-apidoc arimo --output-dir docs --force --module-first --maxdepth 4

# get rid of undocumented members
# grep -C2 ":undoc-members:" docs/arimo*.rst
sed -e /:undoc-members:/d -i .orig docs/arimo*.rst
rm docs/*.orig