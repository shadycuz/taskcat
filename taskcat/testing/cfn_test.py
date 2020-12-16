import logging
from pathlib import Path
from typing import List as ListType, Union

from taskcat._cfn._log_stack_events import _CfnLogTools
from taskcat._cfn.threaded import Stacker
from taskcat._cfn_lint import Lint as TaskCatLint
from taskcat._client_factory import Boto3Cache
from taskcat._config import Config
from taskcat._generate_reports import ReportBuilder
from taskcat._lambda_build import LambdaBuild
from taskcat._s3_stage import stage_in_s3
from taskcat._tui import TerminalPrinter
from taskcat.exceptions import TaskCatException

from .base_test import BaseTest

LOG = logging.getLogger(__name__)


class CFNTest(BaseTest):  # pylint: disable=too-many-instance-attributes
    """
    Manages the lifecycle of AWS resources while running a test.
    """

    def __init__(
        self,
        config: Config,
        printer: Union[TerminalPrinter, None] = None,
        test_names: str = "ALL",
        regions: str = "ALL",
        skip_upload: bool = False,
        lint_disable: bool = False,
        no_delete: bool = False,
        keep_failed: bool = False,
        dont_wait_for_delete: bool = True,
    ):
        """
        Creates a TestManager that manages the lifecycle of AWS resources while running
        a test.

        This instance returned has an additonal property test_definiton that is set
        after the run method is called.

        :param config: takes a taskcat Config object.
        :param printer: an object capable of reporting test progress
        """
        super().__init__(config)
        self.test_definition: Stacker
        self.test_names = test_names
        self.regions = regions
        self.skip_upload = skip_upload
        self.lint_disable = lint_disable
        self.no_delete = no_delete
        self.keep_failed = keep_failed
        self.dont_wait_for_delete = dont_wait_for_delete

        if printer is None:
            self.printer = TerminalPrinter(minimalist=True)
        else:
            self.printer = printer

    def run(self) -> None:
        """
        Starts a Taskcat test run by creating the required resources in AWS.

        :param test_names: comma separated list of tests to run
        :param regions: comma separated list of regions to test in
        :param skip_upload: Use templates in an existing cloudformation bucket.
        :param lint_disable: disable cfn-lint checks
        """

        _trim_regions(self.regions, self.config)
        _trim_tests(self.test_names, self.config)

        boto3_cache = Boto3Cache()

        templates = self.config.get_templates()

        if self.skip_upload and not self.config.config.project.s3_bucket:
            raise TaskCatException(
                "cannot skip_buckets without specifying s3_bucket in config"
            )

        buckets = self.config.get_buckets(boto3_cache)

        if not self.skip_upload:
            # 1. lint
            if not self.lint_disable:
                lint = TaskCatLint(self.config, templates)
                errors = lint.lints[1]
                lint.output_results()
                if errors or not lint.passed:
                    raise TaskCatException("Lint failed with errors")
            # 2. build lambdas
            if self.config.config.project.package_lambda:
                LambdaBuild(self.config, self.config.project_root)
            # 3. s3 sync
            stage_in_s3(
                buckets, self.config.config.project.name, self.config.project_root
            )

        regions = self.config.get_regions(boto3_cache)
        parameters = self.config.get_rendered_parameters(buckets, regions, templates)
        tests = self.config.get_tests(templates, regions, buckets, parameters)

        self.test_definition = Stacker(
            self.config.config.project.name,
            tests,
            shorten_stack_name=self.config.config.project.shorten_stack_name,
        )
        self.test_definition.create_stacks()

        self.printer.report_test_progress(stacker=self.test_definition)

        self.passed = True
        self.result = self.test_definition.stacks

    def clean_up(self) -> None:
        """
        Ends a Taskcat test run by deleting the test related resources in AWS.

        :param no_delete: don't delete stacks or buckets
        :param keep_failed: do not delete failed stacks
        :param dont_wait_for_delete: returns immediately after calling stack_delete
        """

        status = self.test_definition.status()

        # Delete Stacks
        if self.no_delete:
            LOG.info("Skipping delete due to cli argument")
        elif self.keep_failed:
            if len(status["COMPLETE"]) > 0:
                LOG.info("deleting successful stacks")
                self.test_definition.delete_stacks({"status": "CREATE_COMPLETE"})
        else:
            self.test_definition.delete_stacks()

        if not self.dont_wait_for_delete:
            self.printer.report_test_progress(stacker=self.test_definition)

        # TODO: summarise stack statusses (did they complete/delete ok) and print any
        #  error events

        # Delete Templates and Buckets
        buckets = self.config.get_buckets()

        if not self.no_delete or (
            self.keep_failed is True and len(status["FAILED"]) == 0
        ):
            deleted: ListType[str] = []
            for test in buckets.values():
                for bucket in test.values():
                    if (bucket.name not in deleted) and not bucket.regional_buckets:
                        bucket.delete(delete_objects=True)
                        deleted.append(bucket.name)

        # 9. raise if something failed
        # - grabbing the status again to ensure everything deleted OK.

        status = self.test_definition.status()
        if len(status["FAILED"]) > 0:
            raise TaskCatException(
                f'One or more stacks failed to create: {status["FAILED"]}'
            )

    def report(
        self, output_directory: str = "./taskcat_outputs",
    ):
        """
        Generates a report of the status of Cloudformation stacks.

        :param output_directory: Where to store generated logfiles
        """
        report_path = Path(output_directory).resolve()
        report_path.mkdir(exist_ok=True)
        cfn_logs = _CfnLogTools()
        cfn_logs.createcfnlogs(self.test_definition, report_path)
        ReportBuilder(
            self.test_definition, report_path / "index.html"
        ).generate_report()


def _trim_regions(regions, config):
    if regions != "ALL":
        for test in config.config.tests.values():
            to_pop = []
            idx = 0
            if test.regions:
                for _ in test.regions:
                    if test.regions[idx] not in regions.split(","):
                        to_pop.append(idx)
                    idx += 1
                to_pop.reverse()
                for idx in to_pop:
                    test.regions.pop(idx)


def _trim_tests(test_names, config):
    if test_names != "ALL":
        for test in list(config.config.tests):
            if test not in test_names.split(","):
                del config.config.tests[test]
