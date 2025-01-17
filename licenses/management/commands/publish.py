# Standard library
import logging
import os
import socket
from argparse import ArgumentParser
from shutil import copyfile, rmtree

# Third-party
import git
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.management import BaseCommand, CommandError
from django.http.response import Http404
from django.urls import reverse

# First-party/Local
from licenses.git_utils import commit_and_push_changes, setup_local_branch
from licenses.models import LegalCode, TranslationBranch
from licenses.utils import (
    init_utils_logger,
    relative_symlink,
    save_url_as_static_file,
)

LOG = logging.getLogger(__name__)
LOG_LEVELS = {
    0: logging.ERROR,
    1: logging.WARNING,
    2: logging.INFO,
    3: logging.DEBUG,
}


def list_open_translation_branches():
    """
    Return list of names of open translation branches
    """
    return list(
        TranslationBranch.objects.filter(complete=False).values_list(
            "branch_name", flat=True
        )
    )


class Command(BaseCommand):
    """
    Command to push the static files in the build directory to a specified
    branch in cc-licenses-data repository

    Arguments:
        branch_name - Branch name in cc-license-data to pull translations from
                      and publish artifacts too.
        list_branches - A list of active branches in cc-licenses-data will be
                        displayed

    If no arguments are supplied all cc-licenses-data branches are checked and
    then updated.
    """

    def add_arguments(self, parser: ArgumentParser):
        parser.add_argument(
            "-b",
            "--branch_name",
            help="Translation branch name to pull translations from and push"
            " artifacts to. Use --list_branches to see available branch names."
            " With no option, all active branches are published.",
        )
        parser.add_argument(
            "-l",
            "--list_branches",
            action="store_true",
            help="A list of active translation branches will be displayed.",
        )
        parser.add_argument(
            "--nopush",
            action="store_true",
            help="Update the local branches, but don't push upstream.",
        )
        parser.add_argument(
            "--nogit",
            action="store_true",
            help="Update the local files without any attempt to manage them in"
            " git (implies --nopush)",
        )

    def _quiet(self, *args, **kwargs):
        pass

    def run_clean_output_dir(self):
        output_dir = self.output_dir
        output_dir_items = [
            os.path.join(output_dir, item)
            for item in os.listdir(output_dir)
            if item != "CNAME"
        ]
        for item in output_dir_items:
            if os.path.isdir(item):
                rmtree(item)
            else:
                os.remove(item)

    def run_django_distill(self):
        """Outputs static files into the output dir."""
        if not os.path.isdir(settings.STATIC_ROOT):
            e = "Static source directory does not exist, run collectstatic"
            raise CommandError(e)
        hostname = socket.gethostname()
        output_dir = self.output_dir

        LOG.debug(f"{hostname}:{output_dir}")
        save_url_as_static_file(
            output_dir,
            url="/dev/status/",
            relpath="status/index.html",
            html=True,
        )
        tbranches = TranslationBranch.objects.filter(complete=False)
        for tbranch_id in tbranches.values_list("id", flat=True):
            relpath = f"status/{tbranch_id}.html"
            LOG.debug(f"    {relpath}")
            save_url_as_static_file(
                output_dir,
                url=f"/status/{tbranch_id}/",
                relpath=relpath,
                html=True,
            )

        legal_codes = LegalCode.objects.validgroups()
        for group in legal_codes.keys():
            LOG.info(f"Publishing {group}")
            LOG.debug(f"{hostname}:{output_dir}")
            for legal_code in legal_codes[group]:
                # deed
                try:
                    relpath, symlinks = legal_code.get_file_and_links("deed")
                    save_url_as_static_file(
                        output_dir,
                        url=legal_code.deed_url,
                        relpath=relpath,
                        html=True,
                    )
                    for symlink in symlinks:
                        relative_symlink(output_dir, relpath, symlink)
                except Http404 as e:
                    if "invalid language" not in str(e):
                        raise
                # legalcode
                relpath, symlinks = legal_code.get_file_and_links("legalcode")
                save_url_as_static_file(
                    output_dir,
                    url=legal_code.legal_code_url,
                    relpath=relpath,
                    html=True,
                )
                for symlink in symlinks:
                    relative_symlink(output_dir, relpath, symlink)

        LOG.debug(f"{hostname}:{output_dir}")
        save_url_as_static_file(
            output_dir,
            url=reverse("metadata"),
            relpath="licenses/metadata.yaml",
        )

    def run_copy_licenses_rdfs(self):
        hostname = socket.gethostname()
        legacy_dir = self.legacy_dir
        output_dir = self.output_dir
        licenses_rdf_dir = os.path.join(legacy_dir, "rdf-licenses")
        licenses_rdfs = [
            rdf_file
            for rdf_file in os.listdir(licenses_rdf_dir)
            if os.path.isfile(os.path.join(licenses_rdf_dir, rdf_file))
        ]
        licenses_rdfs.sort()
        LOG.info("Publishing legal code RDFs")
        LOG.debug(f"{hostname}:{output_dir}")
        for rdf in licenses_rdfs:
            if rdf.endswith(".rdf"):
                name = rdf[:-4]
            else:
                continue
            relative_name = os.path.join(*name.split("_"), "rdf")
            # "xu" is a "user assigned code" meaning "unported"
            # See https://en.wikipedia.org/wiki/ISO_3166-1_alpha-2#User-assigned_code_elements.  # noqa: E501
            relative_name = relative_name.replace("xu/", "")
            dest_file = os.path.join(output_dir, relative_name)
            os.makedirs(os.path.dirname(dest_file), exist_ok=True)
            copyfile(os.path.join(licenses_rdf_dir, rdf), dest_file)
            LOG.debug(f"    {relative_name}")

    def run_copy_meta_rdfs(self):
        hostname = socket.gethostname()
        legacy_dir = self.legacy_dir
        output_dir = self.output_dir
        meta_rdf_dir = os.path.join(legacy_dir, "rdf-meta")
        meta_files = [
            meta_file
            for meta_file in os.listdir(meta_rdf_dir)
            if os.path.isfile(os.path.join(meta_rdf_dir, meta_file))
        ]
        meta_files.sort()
        dest_dir = os.path.join(output_dir, "rdf")
        os.makedirs(dest_dir, exist_ok=True)
        LOG.info("Publishing RDF information and metadata")
        LOG.debug(f"{hostname}:{output_dir}")
        for meta_file in meta_files:
            dest_relative = os.path.join("rdf", meta_file)
            dest_full = os.path.join(output_dir, dest_relative)
            LOG.debug(f"    {dest_relative}")
            copyfile(os.path.join(meta_rdf_dir, meta_file), dest_full)
            if meta_file == "index.rdf":
                os.makedirs(
                    os.path.join(output_dir, "licenses"), exist_ok=True
                )
                dir_fd = os.open(output_dir, os.O_RDONLY)
                symlink = os.path.join("licenses", meta_file)
                try:
                    os.symlink(f"../{dest_relative}", symlink, dir_fd=dir_fd)
                    LOG.debug(f"   ^{symlink}")
                finally:
                    os.close(dir_fd)
            elif meta_file == "ns.html":
                dir_fd = os.open(output_dir, os.O_RDONLY)
                symlink = meta_file
                try:
                    os.symlink(dest_relative, symlink, dir_fd=dir_fd)
                    LOG.debug(f"   ^{symlink}")
                finally:
                    os.close(dir_fd)
            elif meta_file == "schema.rdf":
                dir_fd = os.open(output_dir, os.O_RDONLY)
                symlink = meta_file
                try:
                    os.symlink(dest_relative, symlink, dir_fd=dir_fd)
                    LOG.debug(f"   ^{symlink}")
                finally:
                    os.close(dir_fd)

    def run_copy_legal_code_plaintext(self):
        hostname = socket.gethostname()
        legacy_dir = self.legacy_dir
        output_dir = self.output_dir
        plaintext_dir = os.path.join(legacy_dir, "legalcode")
        plaintext_files = [
            text_file
            for text_file in os.listdir(plaintext_dir)
            if (
                os.path.isfile(os.path.join(plaintext_dir, text_file))
                and text_file.endswith(".txt")
            )
        ]
        LOG.info("Publishing plaintext legal code")
        LOG.debug(f"{hostname}:{output_dir}")
        for text in plaintext_files:
            if text.startswith("by"):
                context = "licenses"
            else:
                context = "publicdomain"
            name = text[:-4]
            relative_name = os.path.join(
                context,
                *name.split("_"),
                "legalcode.txt",
            )
            dest_file = os.path.join(output_dir, relative_name)
            os.makedirs(os.path.dirname(dest_file), exist_ok=True)
            copyfile(os.path.join(plaintext_dir, text), dest_file)
            LOG.debug(f"    {relative_name}")

    def distill_and_copy(self):
        self.run_clean_output_dir()
        self.run_django_distill()
        self.run_copy_licenses_rdfs()
        self.run_copy_meta_rdfs()
        self.run_copy_legal_code_plaintext()

    def publish_branch(self, branch: str):
        """Workflow for publishing a single branch"""
        LOG.debug(f"Publishing branch {branch}")
        with git.Repo(settings.DATA_REPOSITORY_DIR) as repo:
            setup_local_branch(repo, branch)
            self.distill_and_copy()
            if repo.is_dirty(untracked_files=True):
                # Add any changes and new files

                commit_and_push_changes(
                    repo,
                    "Updated built HTML files",
                    self.relpath,
                    push=self.push,
                )
                if repo.is_dirty(untracked_files=True):
                    raise git.exc.RepositoryDirtyError(
                        settings.DATA_REPOSITORY_DIR,
                        "Repository is dirty. We cannot continue.",
                    )
            else:
                LOG.debug(f"{branch} build dir is up to date.")

    def publish_all(self):
        """Workflow for checking branches and updating their build dir"""
        branches = list_open_translation_branches()
        LOG.info(
            f"Checking and updating build dirs for {len(branches)}"
            " translation branches."
        )
        for branch in branches:
            self.publish_branch(branch)

    def handle(self, *args, **options):
        LOG.setLevel(LOG_LEVELS[int(options["verbosity"])])
        init_utils_logger(LOG)
        self.options = options
        self.output_dir = os.path.abspath(settings.DISTILL_DIR)
        self.legacy_dir = os.path.abspath(settings.LEGACY_DIR)
        git_dir = os.path.abspath(settings.DATA_REPOSITORY_DIR)
        if not self.output_dir.startswith(git_dir):
            raise ImproperlyConfigured(
                "In Django settings, DISTILL_DIR must be inside"
                f" DATA_REPOSITORY_DIR, but DISTILL_DIR={self.output_dir} is"
                f" outside DATA_REPOSITORY_DIR={git_dir}."
            )

        self.relpath = os.path.relpath(self.output_dir, git_dir)
        self.push = not options["nopush"]

        if options.get("list_branches"):
            branches = list_open_translation_branches()
            LOG.debug("Which branch are we publishing to?")
            for branch in branches:
                LOG.debug(branch)
        elif options.get("nogit"):
            self.distill_and_copy()
        elif options.get("branch_name"):
            self.publish_branch(options["branch_name"])
        else:
            self.publish_all()
