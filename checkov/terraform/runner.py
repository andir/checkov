import dataclasses
import logging
import os
from typing import Dict, Optional, Tuple

import dpath.util

from checkov.common.output.record import Record
from checkov.common.output.report import Report
from checkov.common.util import dict_utils
from checkov.common.runners.base_runner import BaseRunner
from checkov.common.variables.context import EvaluationContext
from checkov.runner_filter import RunnerFilter
from checkov.terraform.checks.data.registry import data_registry
from checkov.terraform.checks.module.registry import module_registry
from checkov.terraform.checks.provider.registry import provider_registry
from checkov.terraform.checks.resource.registry import resource_registry
from checkov.terraform.context_parsers.registry import parser_registry
from checkov.terraform.evaluation.base_variable_evaluation import BaseVariableEvaluation
from checkov.terraform.parser import Parser

# Allow the evaluation of empty variables
from checkov.terraform.tag_providers import get_resource_tags

dpath.options.ALLOW_EMPTY_STRING_KEYS = True

CHECK_BLOCK_TYPES = frozenset(['resource', 'data', 'provider', 'module'])


class Runner(BaseRunner):
    check_type = "terraform"

    def __init__(self, parser=Parser()):
        self.parser = parser
        self.tf_definitions = {}
        self.definitions_context = {}
        self.evaluations_context: Dict[str, Dict[str, EvaluationContext]] = {}

    block_type_registries = {
        'resource': resource_registry,
        'data': data_registry,
        'provider': provider_registry,
        'module': module_registry,
    }

    def run(self, root_folder, external_checks_dir=None, files=None, runner_filter=RunnerFilter(), collect_skip_comments=True):
        report = Report(self.check_type)
        self.tf_definitions = {}
        parsing_errors = {}
        if external_checks_dir:
            for directory in external_checks_dir:
                resource_registry.load_external_checks(directory, runner_filter)
        if root_folder:
            root_folder = os.path.abspath(root_folder)
            
            self.parser.parse_directory(directory=root_folder,
                                        out_definitions=self.tf_definitions,
                                        out_evaluations_context=self.evaluations_context,
                                        out_parsing_errors=parsing_errors,
                                        download_external_modules=runner_filter.download_external_modules,
                                        external_modules_download_path=runner_filter.external_modules_download_path,
                                        evaluate_variables=runner_filter.evaluate_variables)
            self.check_tf_definition(report, root_folder, runner_filter, collect_skip_comments)

        if files:
            files = [os.path.abspath(file) for file in files]
            root_folder = os.path.split(os.path.commonprefix(files))[0]
            for file in files:
                if file.endswith(".tf"):
                    file_parsing_errors = {}
                    parse_result = self.parser.parse_file(file=file, parsing_errors=file_parsing_errors)
                    if parse_result is not None:
                        self.tf_definitions[file] = parse_result
                    if file_parsing_errors:
                        parsing_errors.update(file_parsing_errors)
                        continue
                    self.check_tf_definition(report, root_folder, runner_filter, collect_skip_comments)

        report.add_parsing_errors(parsing_errors.keys())

        return report

    def check_tf_definition(self, report, root_folder, runner_filter, collect_skip_comments=True, external_definitions_context=None):
        parser_registry.reset_definitions_context()
        if external_definitions_context:
            definitions_context = external_definitions_context
        else:
            definitions_context = {}
            for definition in self.tf_definitions.items():
                definitions_context = parser_registry.enrich_definitions_context(definition, collect_skip_comments)
            self.definitions_context = definitions_context
            logging.debug('Created definitions context')

        for full_file_path, definition in self.tf_definitions.items():
            abs_scanned_file, abs_referrer = self._strip_module_referrer(full_file_path)
            scanned_file = f"/{os.path.relpath(abs_scanned_file, root_folder)}"
            logging.debug(f"Scanning file: {scanned_file}")
            self.run_all_blocks(definition, definitions_context, full_file_path, root_folder, report,
                                scanned_file, runner_filter, abs_referrer)

    def run_all_blocks(self, definition, definitions_context, full_file_path, root_folder, report,
                       scanned_file, runner_filter, module_referrer: Optional[str]):
        if not definition:
            logging.debug("Empty definition, skipping run (root_folder=%s)", root_folder)
            return
        for block_type in definition.keys():
            if block_type in CHECK_BLOCK_TYPES:
                self.run_block(definition[block_type], definitions_context,
                               full_file_path, root_folder, report,
                               scanned_file, block_type, runner_filter, None, module_referrer)

    def run_block(self, entities,
                  definition_context,
                  full_file_path, root_folder, report, scanned_file,
                  block_type, runner_filter=None, entity_context_path_header=None,
                  module_referrer: Optional[str] = None):

        registry = self.block_type_registries[block_type]
        if not registry:
            return

        for entity in entities:
            entity_evaluations = None
            context_parser = parser_registry.context_parsers[block_type]
            definition_path = context_parser.get_entity_context_path(entity)
            entity_id = ".".join(definition_path)       # example: aws_s3_bucket.my_bucket

            caller_file_path = None
            caller_file_line_range = None

            if module_referrer is not None:
                referrer_id = self._find_id_for_referrer(full_file_path,
                                                         self.tf_definitions)
                if referrer_id:
                    entity_id = f"{referrer_id}.{entity_id}"        # ex: module.my_module.aws_s3_bucket.my_bucket
                    abs_caller_file = module_referrer[:module_referrer.rindex("#")]
                    caller_file_path = f"/{os.path.relpath(abs_caller_file, root_folder)}"

                    try:
                        caller_context = dpath.get(definition_context[abs_caller_file],
                                                   # HACK ALERT: module data is currently double-nested in
                                                   #             definition context. If fixed, remove the
                                                   #             addition of "module." at the beginning.
                                                   "module." + referrer_id,
                                                   separator=".")
                    except KeyError:
                        logging.debug("Unable to find caller context for: %s", abs_caller_file)
                        caller_context = None

                    if caller_context:
                        caller_file_line_range = [caller_context.get('start_line'), caller_context.get('end_line')]
                else:
                    logging.debug(f"Unable to find referrer ID for full path: %s", full_file_path)

            if entity_context_path_header is None:
                entity_context_path = [block_type] + definition_path
            else:
                entity_context_path = entity_context_path_header + block_type + definition_path
            # Entity can exist only once per dir, for file as well
            try:
                entity_context = dict_utils.getInnerDict(definition_context[full_file_path], entity_context_path)
                entity_lines_range = [entity_context.get('start_line'), entity_context.get('end_line')]
                entity_code_lines = entity_context.get('code_lines')
                skipped_checks = entity_context.get('skipped_checks')
            except KeyError:
                # TODO: Context info isn't working for modules
                entity_lines_range = None
                entity_code_lines = None
                skipped_checks = None

            if full_file_path in self.evaluations_context:
                variables_evaluations = {}
                for var_name, context_info in self.evaluations_context.get(full_file_path, {}).items():
                    variables_evaluations[var_name] = dataclasses.asdict(context_info)
                entity_evaluations = BaseVariableEvaluation.reduce_entity_evaluations(variables_evaluations,
                                                                                      entity_context_path)
            results = registry.scan(scanned_file, entity, skipped_checks, runner_filter)
            absolut_scanned_file_path, _ = self._strip_module_referrer(file_path=full_file_path)
            # This duplicates a call at the start of scan, but adding this here seems better than kludging with some tuple return type
            (entity_type, _, entity_config) = registry.extract_entity_details(entity)
            tags = get_resource_tags(entity_type, entity_config)
            for check, check_result in results.items():
                record = Record(check_id=check.id, check_name=check.name, check_result=check_result,
                                code_block=entity_code_lines, file_path=scanned_file,
                                file_line_range=entity_lines_range,
                                resource=entity_id, evaluations=entity_evaluations,
                                check_class=check.__class__.__module__, file_abs_path=absolut_scanned_file_path,
                                entity_tags=tags,
                                caller_file_path=caller_file_path,
                                caller_file_line_range=caller_file_line_range)
                report.add_record(record=record)

    @staticmethod
    def _strip_module_referrer(file_path: str) -> Tuple[str, Optional[str]]:
        """
        For file paths containing module referrer information (e.g.: "module/module.tf[main.tf#0]"), this
        returns a tuple containing the file path (e.g., "module/module.tf") and referrer (e.g., "main.tf#0").
        If the file path does not contain a referred, the tuple will contain the original file path and None.
        """
        if file_path.endswith("]") and "[" in file_path:
            return file_path[:file_path.index("[")], file_path[file_path.index("[") + 1: -1]
        else:
            return file_path, None

    @staticmethod
    def _find_id_for_referrer(full_file_path, definitions) -> Optional[str]:
        for file, file_content in definitions.items():
            if "module" not in file_content:
                continue

            for modules in file_content["module"]:
                for module_name, module_content in modules.items():
                    if "__resolved__" not in module_content:
                        continue

                    if full_file_path in module_content["__resolved__"]:
                        return f"module.{module_name}"
        return None
