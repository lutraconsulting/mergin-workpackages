
"""
Integration of work package algorithm into Mergin projects

For initial run we need a Mergin project with at least these files:
- data.gpkg                -- master database
- work-packages/config.db  -- how to split the database + what work packages to create

The config.db should contain two tables as defined in config.sql:
- wp_names - defines names of work packages and their corresponding Mergin projects
- wp_tables - defines which tables of the master GPKG should be filtered in work packages

After the initial run, the algorithm will add some more files:
- work-packages/remap.db
- work-packages/master.gpkg
- work-packages/<WP1>.gpkg
- work-packages/<WP2>.gpkg
- work-package/<...>.gpkg
These files are used internally by the algorithm and should not be modified (or deleted).

"""

import getpass
import glob
import mergin
import mergin.client_push
import os
import shutil
import sys
import tempfile

from wp import load_config_from_yaml, make_work_packages

if len(sys.argv) != 2:
    raise ValueError("Need a parameter with master Mergin project name")

master_mergin_project = sys.argv[1]    # e.g.  martin/wp-master

mergin_user = os.getenv('MERGIN_USERNAME')
if mergin_user is None:
    mergin_user = input('Mergin username: ')

mergin_password = os.getenv('MERGIN_PASSWORD')
if mergin_password is None:
    mergin_password = getpass.getpass(f'Password for {mergin_user}: ')

# this will create a directory with a random name, e.g. /tmp/mergin-work-packages-w7tbsyd7
tmp_dir = tempfile.mkdtemp(prefix='mergin-work-packages-')

mc = mergin.MerginClient(login=mergin_user, password=mergin_password)

wp_alg_dir = os.path.join(tmp_dir, 'wp')  # where we expect "base", "input" subdirs
wp_alg_base_dir = os.path.join(wp_alg_dir, 'base')
wp_alg_input_dir = os.path.join(wp_alg_dir, 'input')
wp_alg_output_dir = os.path.join(wp_alg_dir, 'output')
os.makedirs(wp_alg_base_dir)
os.makedirs(wp_alg_input_dir)

master_dir = os.path.join(tmp_dir, 'master')
master_config_yaml = os.path.join(master_dir, 'mergin-work-packages.yml')


def get_master_project_files(directory):
    """ Returns list of relative file names from the master project that should be copied to the new WP projects """
    mergin_internal_dir = os.path.join(directory, '.mergin')
    config_file = os.path.join(directory, 'mergin-work-packages.yml')
    wp_dir = os.path.join(directory, 'work-packages')
    files = []
    for filename in glob.iglob(os.path.join(directory, '**'), recursive=True):
        if filename.startswith(mergin_internal_dir) or filename.startswith(wp_dir) or filename == config_file:
            continue
        filename_relative = filename[len(directory)+1:]  # remove prefix
        if len(filename_relative):
            files.append(filename_relative)
    return files

#
# 1. prepare directory with inputs
#    - fetch master mergin project, read configuration in config.db, copy base files and master input file
#    - fetch WP projects and copy their input files
#


print("Downloading master project " + master_mergin_project + "...")
mc.download_project(master_mergin_project, master_dir)
print("Done.")

print("Reading configuration from " + master_config_yaml)
wp_config = load_config_from_yaml(master_config_yaml)
gpkg_path = wp_config.master_gpkg

shutil.copy(os.path.join(master_dir, gpkg_path), os.path.join(wp_alg_input_dir, 'master.gpkg'))

# the master.gpkg and remap.db should exist if this is not the first run of the tool
if os.path.exists(os.path.join(master_dir, 'work-packages', 'master.gpkg')):
    shutil.copy(os.path.join(master_dir, 'work-packages', 'master.gpkg'), os.path.join(wp_alg_base_dir, 'master.gpkg'))
if os.path.exists(os.path.join(master_dir, 'work-packages', 'remap.db')):
    shutil.copy(os.path.join(master_dir, 'work-packages', 'remap.db'), os.path.join(wp_alg_base_dir, 'remap.db'))

master_project_files = get_master_project_files(master_dir)
assert gpkg_path in master_project_files
master_project_files.remove(gpkg_path)
print("Master project files to copy to new projects: " + str(master_project_files))

# list of WP names that did not exist previously (and we will have to create a new Mergin project for them)
wp_new = set()

for wp in wp_config.wp_names:
    wp_name, wp_value, wp_mergin = wp.name, wp.value, wp.mergin_project
    wp_dir = os.path.join(tmp_dir, 'wp-'+wp_name)

    wp_base_file = os.path.join(master_dir, 'work-packages', wp_name + '.gpkg')
    if os.path.exists(wp_base_file):   # already processed?
        print("Preparing work package " + wp_name)
        shutil.copy(wp_base_file, os.path.join(wp_alg_base_dir, wp_name + '.gpkg'))

        print("Downloading work package project " + wp_mergin + "...")
        mc.download_project(wp_mergin, wp_dir)
        print("Done.")

        shutil.copy(os.path.join(wp_dir, gpkg_path), os.path.join(wp_alg_input_dir, wp_name+'.gpkg'))
    else:
        print("First time encountered WP " + wp_name + " - not collecting input")
        wp_new.add(wp_name)

#
# 2. run alg
#

make_work_packages(wp_alg_dir, wp_config)

#
# 3. push data to all projects
#


def push_mergin_project(mc, directory):
    job = mergin.client_push.push_project_async(mc, directory)
    if job is None:
        return False  # there is nothing to push (or we only deleted some files)
    mergin.client_push.push_project_wait(job)
    mergin.client_push.push_project_finalize(job)
    return True


for wp in wp_config.wp_names:
    wp_name, wp_value, wp_mergin = wp.name, wp.value, wp.mergin_project
    wp_dir = os.path.join(tmp_dir, 'wp-'+wp_name)

    if wp_name in wp_new:
        # we need to create new project
        print("Creating project: " + wp_mergin + " for work package " + wp_name)
        wp_mergin_project_namespace, wp_mergin_project_name = wp_mergin.split('/')
        mc.create_project(wp_mergin_project_name, False, wp_mergin_project_namespace)
        mc.download_project(wp_mergin, wp_dir)

        shutil.copy(os.path.join(wp_alg_output_dir, wp_name+'.gpkg'), os.path.join(wp_dir, gpkg_path))

        # copy other files from master project
        for filename in master_project_files:
            print("Adding file from master project: " + filename)
            # TODO: we may need to create sub-directories
            shutil.copy(os.path.join(master_dir, filename), os.path.join(wp_dir, filename))

    # new version of the geopackage
    shutil.copy(os.path.join(wp_alg_output_dir, wp_name+'.gpkg'), os.path.join(wp_dir, gpkg_path))

    print("Uploading new version of the project: " + wp_mergin + " for work package " + wp_name)
    if not push_mergin_project(mc, wp_dir):
        print("No changes (not creating a new version).")
    else:
        print("Uploaded a new version: " + mergin.MerginProject(wp_dir).metadata['version'])


# in the last step, let's update the master project
# (update the master database file and update base files for work packages)
shutil.copy(os.path.join(wp_alg_output_dir, 'master.gpkg'), os.path.join(master_dir, gpkg_path))
if not os.path.exists(os.path.join(master_dir, 'work-packages')):
    os.makedirs(os.path.join(master_dir, 'work-packages'))
shutil.copy(os.path.join(wp_alg_output_dir, 'master.gpkg'), os.path.join(master_dir, 'work-packages', 'master.gpkg'))
shutil.copy(os.path.join(wp_alg_output_dir, 'remap.db'), os.path.join(master_dir, 'work-packages', 'remap.db'))
for wp in wp_config.wp_names:
    wp_name, wp_value, wp_mergin = wp.name, wp.value, wp.mergin_project
    shutil.copy(os.path.join(wp_alg_output_dir, wp_name+'.gpkg'), os.path.join(master_dir, 'work-packages', wp_name+'.gpkg'))

print("Uploading new version of the master project: " + master_mergin_project)
if not push_mergin_project(mc, master_dir):
    print("No changes (not creating a new version).")
else:
    print("Uploaded a new version: " + mergin.MerginProject(master_dir).metadata['version'])
print("Done.")
