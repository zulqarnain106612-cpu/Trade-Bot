# Trade Bot — Architecture Intelligence
> Auto-generated 2026-07-03 23:32 | 813 Python modules | 235,725 total lines

## System Purpose
Production algorithmic trading bot: Binance (primary) + OKX (secondary).
ML signal stack → risk gates → execution. Paper-first, live-gated.

## Signal Pipeline (data flow order)
```
Exchange OHLCV/OrderBook
  → fetcher.py          [ccxt, 1m/15m/4h]
  → storage.py          [SQLite WAL, async]
  → pipeline.py         [7 features, triple-barrier labels, CPCV]
  → detector.py         [GaussianHMM 3-state regime]
  → trainer.py          [XGBoost direction P(long) + meta-label P(bet)]
  → filters.py          [8 signal filters: EWM/Hurst/OBV/ATR/MTF]
  → signal_engine.py    [per-timeframe signal score]
  → position_sizing.py  [Half-Kelly + Carver + AFML + Thorp]
  → gates.py            [sequential hard risk gates, short-circuit]
  → paper.py / live.py  [execution: Auto/Restricted/Manual]
  → orchestrator.py     [async event loop]
  → main.py             [FastAPI + WebSocket]
  → React dashboard     [equity, positions, approvals, regime]
```

## Module Inventory

### `.project-intel/scripts/agent_detect.py` (77 lines)
**Purpose**: Agent Detector
==============
Detects which agent is currently active from envir
**Key functions**: detec

### `.project-intel/scripts/cognitive_layer.py` (565 lines)
**Purpose**: Cognitive Architecture Layer
==============================
Persistent domain kn
**Key functions**: build

### `.project-intel/scripts/context_builder.py` (220 lines)
**Purpose**: Smart Context Builder
======================
Assembles the MINIMUM context for a
**Key functions**: find_

### `.project-intel/scripts/extract_intelligence.py` (659 lines)
**Purpose**: Project Intelligence Extractor
================================
Transforms a cod
**Key functions**: extra

### `.project-intel/scripts/handoff.py` (364 lines)
**Purpose**: Agent Handoff Manager
======================
Tracks which agent is working, what
**Key functions**: cmd_s

### `.project-intel/scripts/rag_engine.py` (386 lines)
**Purpose**: RAG Engine — BM25 on SQLite, zero external dependencies
========================
**Classes**: BM25Index
**Key functions**: token

### `.project-intel/scripts/resume.py` (193 lines)
**Purpose**: SESSION RESUME — single command, zero follow-up file reads.

Outputs ONE compres
**Key functions**: git_s

### `.project-intel/scripts/update_session.py` (106 lines)
**Purpose**: Session State Updater
======================
Agents run this at the END of every
**Key functions**: main

### `.venv_temp/lib/python3.14/site-packages/_distutils_hack/__init__.py` (239 lines)
**Purpose**: __init__ module
**Classes**: _TrivialRe, DistutilsMetaFinder, shim, DistutilsLoader
**Key functions**: warn_

### `.venv_temp/lib/python3.14/site-packages/_distutils_hack/override.py` (1 lines)
**Purpose**: override module

### `.venv_temp/lib/python3.14/site-packages/packaging/__init__.py` (15 lines)
**Purpose**: __init__ module

### `.venv_temp/lib/python3.14/site-packages/packaging/_elffile.py` (108 lines)
**Purpose**: ELF file parser.

This provides a class ``ELFFile`` that parses an ELF executabl
**Classes**: ELFInvalid, EIClass, EIData, EMachine, ELFFile

### `.venv_temp/lib/python3.14/site-packages/packaging/_manylinux.py` (262 lines)
**Purpose**: _manylinux module
**Classes**: _GLibCVersion
**Key functions**: platf

### `.venv_temp/lib/python3.14/site-packages/packaging/_musllinux.py` (85 lines)
**Purpose**: PEP 656 support.

This module implements logic to detect if the currently runnin
**Classes**: _MuslVersion
**Key functions**: platf

### `.venv_temp/lib/python3.14/site-packages/packaging/_parser.py` (393 lines)
**Purpose**: Handwritten parser of dependency specifiers.

The docstring for each __parse_* f
**Classes**: Node, Variable, Value, Op, ParsedRequirement
**Key functions**: parse

### `.venv_temp/lib/python3.14/site-packages/packaging/_structures.py` (33 lines)
**Purpose**: Backward-compatibility shim for unpickling Version objects serialized before
pac
**Classes**: InfinityType, NegativeInfinityType

### `.venv_temp/lib/python3.14/site-packages/packaging/_tokenizer.py` (193 lines)
**Purpose**: _tokenizer module
**Classes**: Token, ParserSyntaxError, Tokenizer

### `.venv_temp/lib/python3.14/site-packages/packaging/dependency_groups.py` (302 lines)
**Purpose**: dependency_groups module
**Classes**: DuplicateGroupNames, CyclicDependencyGroup, InvalidDependencyGroupObject, DependencyGroupInclude, DependencyGroupResolver
**Key functions**: resol

### `.venv_temp/lib/python3.14/site-packages/packaging/direct_url.py` (325 lines)
**Purpose**: direct_url module
**Classes**: _FromMappingProtocol, DirectUrlValidationError, _DirectUrlRequiredKeyError, VcsInfo, ArchiveInfo, DirInfo, DirectUrl

### `.venv_temp/lib/python3.14/site-packages/packaging/errors.py` (94 lines)
**Purpose**: errors module
**Classes**: _ErrorCollector, ExceptionGroup

### `.venv_temp/lib/python3.14/site-packages/packaging/licenses/__init__.py` (186 lines)
**Purpose**: __init__ module
**Classes**: InvalidLicenseExpression
**Key functions**: canon

### `.venv_temp/lib/python3.14/site-packages/packaging/licenses/_spdx.py` (799 lines)
**Purpose**: _spdx module
**Classes**: SPDXLicense, SPDXException

### `.venv_temp/lib/python3.14/site-packages/packaging/markers.py` (492 lines)
**Purpose**: markers module
**Classes**: InvalidMarker, UndefinedComparison, UndefinedEnvironmentName, Environment, Marker
**Key functions**: defau

### `.venv_temp/lib/python3.14/site-packages/packaging/metadata.py` (964 lines)
**Purpose**: metadata module
**Classes**: InvalidMetadata, RawMetadata, RFC822Policy, RFC822Message, _Validator, Metadata
**Key functions**: parse

### `.venv_temp/lib/python3.14/site-packages/packaging/pylock.py` (905 lines)
**Purpose**: pylock module
**Classes**: _FromMappingProtocol, PylockValidationError, _PylockRequiredKeyError, PylockUnsupportedVersionError, PylockSelectError, PackageVcs, PackageDirectory, PackageArchive, PackageSdist, PackageWheel, Package, Pylock
**Key functions**: is_va

### `.venv_temp/lib/python3.14/site-packages/packaging/requirements.py` (129 lines)
**Purpose**: requirements module
**Classes**: InvalidRequirement, Requirement

### `.venv_temp/lib/python3.14/site-packages/packaging/specifiers.py` (1943 lines)
**Purpose**: .. testsetup::

    from packaging.specifiers import Specifier, SpecifierSet, In
**Classes**: _BoundaryKind, _BoundaryVersion, _LowerBound, _UpperBound, InvalidSpecifier, BaseSpecifier, Specifier, SpecifierSet

### `.venv_temp/lib/python3.14/site-packages/packaging/tags.py` (932 lines)
**Purpose**: tags module
**Classes**: UnsortedTagsError, Tag
**Key functions**: parse

### `.venv_temp/lib/python3.14/site-packages/packaging/utils.py` (296 lines)
**Purpose**: utils module
**Classes**: InvalidName, InvalidWheelFilename, InvalidSdistFilename
**Key functions**: canon

### `.venv_temp/lib/python3.14/site-packages/packaging/version.py` (1231 lines)
**Purpose**: .. testsetup::

    from packaging.version import parse, normalize_pre, Version,
**Classes**: _VersionReplace, InvalidVersion, _BaseVersion, _Version, Version, _TrimmedRelease
**Key functions**: norma

### `.venv_temp/lib/python3.14/site-packages/pip/__init__.py` (13 lines)
**Purpose**: __init__ module
**Key functions**: main

### `.venv_temp/lib/python3.14/site-packages/pip/__main__.py` (24 lines)
**Purpose**: __main__ module

### `.venv_temp/lib/python3.14/site-packages/pip/__pip-runner__.py` (50 lines)
**Purpose**: Execute exactly this copy of pip, within a different environment.

This file is 
**Classes**: PipImportRedirectingFinder
**Key functions**: versi

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/__init__.py` (18 lines)
**Purpose**: __init__ module
**Key functions**: main

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/build_env.py` (606 lines)
**Purpose**: Build Environment used for isolation during sdist building
**Classes**: _Prefix, BuildEnvironmentInstaller, SubprocessBuildEnvironmentInstaller, InprocessBuildEnvironmentInstaller, BuildEnvironment, NoOpBuildEnvironment, ExtraEnviron
**Key functions**: get_r

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/cache.py` (291 lines)
**Purpose**: Cache Management
**Classes**: Cache, SimpleWheelCache, EphemWheelCache, CacheEntry, WheelCache

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/cli/__init__.py` (3 lines)
**Purpose**: Subpackage containing all of pip's command line interface related code

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/cli/autocompletion.py` (184 lines)
**Purpose**: Logic that powers autocompletion installed by ``pip completion``.
**Key functions**: autoc

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/cli/base_command.py` (264 lines)
**Purpose**: Base Command class, and related routines
**Classes**: Command

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/cli/cmdoptions.py` (1298 lines)
**Purpose**: shared options and groups

The principle here is to define options once, but *no
**Classes**: PipOption
**Key functions**: raise

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/cli/command_context.py` (28 lines)
**Purpose**: command_context module
**Classes**: CommandContextMixIn

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/cli/index_command.py` (212 lines)
**Purpose**: Contains command classes which may interact with an index / the network.

Unlike
**Classes**: SessionCommandMixin, IndexGroupCommand

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/cli/main.py` (85 lines)
**Purpose**: Primary application entrypoint.
**Key functions**: main

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/cli/main_parser.py` (136 lines)
**Purpose**: A single place for constructing and exposing the main parser
**Key functions**: creat

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/cli/parser.py` (358 lines)
**Purpose**: Base option parser setup
**Classes**: PrettyHelpFormatter, UpdatingDefaultsHelpFormatter, CustomOptionParser, ConfigOptionParser

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/cli/progress_bars.py` (153 lines)
**Purpose**: progress_bars module
**Key functions**: get_d

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/cli/req_command.py` (472 lines)
**Purpose**: Contains the RequirementCommand base class.

This class is in a separate module 
**Classes**: RequirementCommand
**Key functions**: shoul

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/cli/spinners.py` (235 lines)
**Purpose**: spinners module
**Classes**: SpinnerInterface, InteractiveSpinner, NonInteractiveSpinner, RateLimiter, _PipRichSpinner
**Key functions**: open_

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/cli/status_codes.py` (6 lines)
**Purpose**: status_codes module

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/commands/__init__.py` (139 lines)
**Purpose**: Package containing all pip commands
**Key functions**: creat

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/commands/cache.py` (255 lines)
**Purpose**: cache module
**Classes**: CacheCommand

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/commands/check.py` (66 lines)
**Purpose**: check module
**Classes**: CheckCommand

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/commands/completion.py` (136 lines)
**Purpose**: completion module
**Classes**: CompletionCommand

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/commands/configuration.py` (288 lines)
**Purpose**: configuration module
**Classes**: ConfigurationCommand

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/commands/debug.py` (196 lines)
**Purpose**: debug module
**Classes**: DebugCommand
**Key functions**: show_

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/commands/download.py` (146 lines)
**Purpose**: download module
**Classes**: DownloadCommand

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/commands/freeze.py` (107 lines)
**Purpose**: freeze module
**Classes**: FreezeCommand

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/commands/hash.py` (58 lines)
**Purpose**: hash module
**Classes**: HashCommand

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/commands/help.py` (40 lines)
**Purpose**: help module
**Classes**: HelpCommand

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/commands/index.py` (166 lines)
**Purpose**: index module
**Classes**: IndexCommand

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/commands/inspect.py` (92 lines)
**Purpose**: inspect module
**Classes**: InspectCommand

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/commands/install.py` (904 lines)
**Purpose**: install module
**Classes**: InstallCommand
**Key functions**: insta

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/commands/list.py` (403 lines)
**Purpose**: list module
**Classes**: ListCommand, _DistWithLatestInfo
**Key functions**: forma

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/commands/lock.py` (175 lines)
**Purpose**: lock module
**Classes**: LockCommand

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/commands/search.py` (178 lines)
**Purpose**: search module
**Classes**: TransformedHit, SearchCommand
**Key functions**: trans

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/commands/show.py` (231 lines)
**Purpose**: show module
**Classes**: ShowCommand, _PackageInfo
**Key functions**: norma

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/commands/uninstall.py` (113 lines)
**Purpose**: uninstall module
**Classes**: UninstallCommand

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/commands/wheel.py` (171 lines)
**Purpose**: wheel module
**Classes**: WheelCommand

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/configuration.py` (396 lines)
**Purpose**: Configuration management setup

Some terminology:
- name
  As written in config 
**Classes**: Configuration
**Key functions**: get_c

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/distributions/__init__.py` (21 lines)
**Purpose**: __init__ module
**Key functions**: make_

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/distributions/base.py` (55 lines)
**Purpose**: Abstract executor interface
**Classes**: AbstractDistribution

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/distributions/installed.py` (33 lines)
**Purpose**: installed module
**Classes**: InstalledDistribution

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/distributions/sdist.py` (164 lines)
**Purpose**: sdist module
**Classes**: SourceDistribution

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/distributions/wheel.py` (44 lines)
**Purpose**: wheel module
**Classes**: WheelDistribution

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/exceptions.py` (971 lines)
**Purpose**: Exceptions used throughout package.

This module MUST NOT try to import from any
**Classes**: PipError, DiagnosticPipError, ConfigurationError, InstallationError, FailedToPrepareCandidate, MissingPyProjectBuildRequires, InvalidPyProjectBuildRequires, NoneMetadataError, UserInstallationInvalid, InvalidSchemeCombination, DistributionNotFound, RequirementsFileParseError, BestVersionAlreadyInstalled, BadCommand, CommandError, PreviousBuildDirError, NetworkConnectionError, InvalidWheelFilename, UnsupportedWheel, InvalidWheel, MetadataInconsistent, MetadataInvalid, InstallationSubprocessError, MetadataGenerationFailed, HashErrors, HashError, VcsHashUnsupported, DirectoryUrlHashUnsupported, HashMissing, HashUnpinned, HashMismatch, UnsupportedPythonVersion, ConfigurationFileCouldNotBeLoaded, ExternallyManagedEnvironment, UninstallMissingRecord, LegacyDistutilsInstall, InvalidInstalledPackage, IncompleteDownloadError, ResolutionTooDeepError, InstallWheelBuildError, InvalidEggFragment, BuildDependencyInstallError

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/index/__init__.py` (1 lines)
**Purpose**: Index interaction code

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/index/collector.py` (488 lines)
**Purpose**: The main purpose of this module is to expose LinkCollector.collect_sources().
**Classes**: _NotAPIContent, _NotHTTP, CacheablePageContent, ParseLinks, IndexContent, HTMLLinkParser, CollectedSources, LinkCollector
**Key functions**: with_

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/index/package_finder.py` (1113 lines)
**Purpose**: Routines related to PyPI, indexes
**Classes**: LinkType, LinkEvaluator, CandidatePreferences, BestCandidateResult, CandidateEvaluator, PackageFinder
**Key functions**: filte

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/index/sources.py` (287 lines)
**Purpose**: sources module
**Classes**: LinkSource, _FlatDirectoryToUrls, _FlatDirectorySource, _LocalFileSource, _RemoteFileSource, _IndexDirectorySource
**Key functions**: build

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/locations/__init__.py` (438 lines)
**Purpose**: __init__ module
**Key functions**: get_s

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/locations/_distutils.py` (173 lines)
**Purpose**: Locations where we look for configs, install stuff, etc
**Key functions**: distu

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/locations/_sysconfig.py` (218 lines)
**Purpose**: _sysconfig module
**Key functions**: get_s

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/locations/base.py` (82 lines)
**Purpose**: Abstract executor interface
**Key functions**: get_m

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/main.py` (12 lines)
**Purpose**: FastAPI REST + WebSocket API
**Key functions**: main

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/metadata/__init__.py` (169 lines)
**Purpose**: __init__ module
**Classes**: Backend
**Key functions**: selec

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/metadata/_json.py` (87 lines)
**Purpose**: _json module
**Key functions**: json_

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/metadata/base.py` (685 lines)
**Purpose**: Abstract executor interface
**Classes**: BaseEntryPoint, RequiresEntry, BaseDistribution, BaseEnvironment, Wheel, FilesystemWheel, MemoryWheel

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/metadata/importlib/__init__.py` (6 lines)
**Purpose**: __init__ module

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/metadata/importlib/_compat.py` (87 lines)
**Purpose**: _compat module
**Classes**: BadMetadata, BasePath
**Key functions**: get_i

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/metadata/importlib/_dists.py` (235 lines)
**Purpose**: _dists module
**Classes**: WheelDistribution, Distribution

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/metadata/importlib/_envs.py` (143 lines)
**Purpose**: _envs module
**Classes**: _DistributionFinder, Environment

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/metadata/pkg_resources.py` (298 lines)
**Purpose**: pkg_resources module
**Classes**: EntryPoint, InMemoryMetadata, Distribution, Environment

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/network/__init__.py` (1 lines)
**Purpose**: Contains purely network-related utilities.

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/network/auth.py` (570 lines)
**Purpose**: Network Authentication Helpers

Contains interface (MultiDomainBasicAuth) and as
**Classes**: Credentials, KeyRingBaseProvider, KeyRingNullProvider, KeyRingPythonProvider, KeyRingCliProvider, MultiDomainBasicAuth
**Key functions**: get_k

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/network/cache.py` (128 lines)
**Purpose**: HTTP cache implementation.
**Classes**: SafeFileCache
**Key functions**: is_fr

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/network/download.py` (340 lines)
**Purpose**: Download files with progress indicators.
**Classes**: _FileDownload, Downloader
**Key functions**: sanit

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/network/lazy_wheel.py` (215 lines)
**Purpose**: Lazy ZIP over HTTP
**Classes**: HTTPRangeRequestUnsupported, LazyZipOverHTTP
**Key functions**: dist_

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/network/session.py` (537 lines)
**Purpose**: PipSession and supporting code, containing all pip-specific
network request conf
**Classes**: LocalFSAdapter, _SSLContextAdapterMixin, HTTPAdapter, CacheControlAdapter, InsecureHTTPAdapter, InsecureCacheControlAdapter, PipSession
**Key functions**: looks

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/network/utils.py` (98 lines)
**Purpose**: utils module
**Key functions**: raise

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/network/xmlrpc.py` (61 lines)
**Purpose**: xmlrpclib.Transport implementation
**Classes**: PipXmlrpcTransport

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/operations/__init__.py` (0 lines)
**Purpose**: __init__ module

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/operations/check.py` (175 lines)
**Purpose**: Validation of dependencies of packages
**Classes**: PackageDetails
**Key functions**: creat

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/operations/freeze.py` (259 lines)
**Purpose**: freeze module
**Classes**: _EditableInfo, FrozenRequirement
**Key functions**: freez

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/operations/install/__init__.py` (1 lines)
**Purpose**: For modules related to installing packages.

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/operations/install/wheel.py` (759 lines)
**Purpose**: Support for installing and building the "wheel" binary package format.
**Classes**: File, ZipBackedFile, ScriptFile, MissingCallableSuffix, PipScriptMaker
**Key functions**: rehas

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/operations/prepare.py` (751 lines)
**Purpose**: Prepares a distribution for installation
**Classes**: File, RequirementPreparer
**Key functions**: unpac

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/pyproject.py` (123 lines)
**Purpose**: pyproject module
**Key functions**: make_

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/req/__init__.py` (103 lines)
**Purpose**: __init__ module
**Classes**: InstallationResult
**Key functions**: insta

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/req/constructors.py` (677 lines)
**Purpose**: Backing implementation for InstallRequirement's various constructors

The idea h
**Classes**: RequirementParts
**Key functions**: conve

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/req/pep723.py` (41 lines)
**Purpose**: pep723 module
**Classes**: PEP723Exception
**Key functions**: pep72

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/req/req_dependency_group.py` (86 lines)
**Purpose**: req_dependency_group module
**Key functions**: parse

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/req/req_file.py` (622 lines)
**Purpose**: Requirements file parsing
**Classes**: ParsedRequirement, ParsedLine, RequirementsFileParser, OptionParsingError
**Key functions**: parse

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/req/req_install.py` (838 lines)
**Purpose**: req_install module
**Classes**: InstallRequirement
**Key functions**: check

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/req/req_set.py` (81 lines)
**Purpose**: req_set module
**Classes**: RequirementSet

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/req/req_uninstall.py` (639 lines)
**Purpose**: req_uninstall module
**Classes**: StashedUninstallPathSet, UninstallPathSet, UninstallPthEntries
**Key functions**: unins

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/resolution/__init__.py` (0 lines)
**Purpose**: __init__ module

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/resolution/base.py` (20 lines)
**Purpose**: Abstract executor interface
**Classes**: BaseResolver

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/resolution/legacy/__init__.py` (0 lines)
**Purpose**: __init__ module

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/resolution/legacy/resolver.py` (598 lines)
**Purpose**: Dependency Resolution

The dependency resolution in pip is performed as follows:
**Classes**: Resolver

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/resolution/resolvelib/__init__.py` (0 lines)
**Purpose**: __init__ module

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/resolution/resolvelib/base.py` (164 lines)
**Purpose**: Abstract executor interface
**Classes**: Constraint, Requirement, Candidate
**Key functions**: forma

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/resolution/resolvelib/candidates.py` (599 lines)
**Purpose**: candidates module
**Classes**: _InstallRequirementBackedCandidate, LinkCandidate, EditableCandidate, AlreadyInstalledCandidate, ExtrasCandidate, RequiresPythonCandidate
**Key functions**: as_ba

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/resolution/resolvelib/factory.py` (914 lines)
**Purpose**: factory module
**Classes**: CollectedRootRequirements, Factory, ConflictCause

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/resolution/resolvelib/found_candidates.py` (166 lines)
**Purpose**: Utilities to lazily create and visit candidates found.

Creating and visiting a 
**Classes**: FoundCandidates

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/resolution/resolvelib/provider.py` (306 lines)
**Purpose**: provider module
**Classes**: PipProvider

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/resolution/resolvelib/reporter.py` (98 lines)
**Purpose**: reporter module
**Classes**: PipReporter, PipDebuggingReporter

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/resolution/resolvelib/requirements.py` (251 lines)
**Purpose**: requirements module
**Classes**: ExplicitRequirement, SpecifierRequirement, SpecifierWithoutExtrasRequirement, RequiresPythonRequirement, UnsatisfiableRequirement

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/resolution/resolvelib/resolver.py` (332 lines)
**Purpose**: resolver module
**Classes**: Resolver
**Key functions**: get_t

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/self_outdated_check.py` (246 lines)
**Purpose**: self_outdated_check module
**Classes**: SelfCheckState, UpgradePrompt
**Key functions**: pip_s

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/utils/__init__.py` (0 lines)
**Purpose**: __init__ module

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/utils/_jaraco_text.py` (109 lines)
**Purpose**: Functions brought over from jaraco.text.

These functions are not supposed to be
**Key functions**: yield

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/utils/_log.py` (38 lines)
**Purpose**: Customize logging

Defines custom logger class for the `logger.verbose(...)` met
**Classes**: VerboseLogger
**Key functions**: getLo

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/utils/appdirs.py` (52 lines)
**Purpose**: This code wraps the vendored appdirs module to so the return values are
compatib
**Key functions**: user_

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/utils/compat.py` (85 lines)
**Purpose**: Stuff that differs in different Python versions and platform
distributions.
**Key functions**: has_t

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/utils/compatibility_tags.py` (201 lines)
**Purpose**: Generate and work with PEP 425 Compatibility Tags.
**Key functions**: versi

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/utils/datetime.py` (28 lines)
**Purpose**: For when pip wants to check the date or time.
**Key functions**: today

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/utils/deprecation.py` (139 lines)
**Purpose**: A module that implements tooling to enable easy warnings about deprecations.
**Classes**: PipDeprecationWarning
**Key functions**: insta

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/utils/direct_url_helpers.py` (92 lines)
**Purpose**: direct_url_helpers module
**Key functions**: direc

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/utils/egg_link.py` (81 lines)
**Purpose**: egg_link module
**Key functions**: egg_l

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/utils/entrypoints.py` (88 lines)
**Purpose**: entrypoints module
**Key functions**: get_b

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/utils/filesystem.py` (201 lines)
**Purpose**: filesystem module
**Key functions**: check

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/utils/filetypes.py` (24 lines)
**Purpose**: Filetype information.
**Key functions**: is_ar

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/utils/glibc.py` (102 lines)
**Purpose**: glibc module
**Key functions**: glibc

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/utils/hashes.py` (150 lines)
**Purpose**: hashes module
**Classes**: Hashes, MissingHashes

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/utils/logging.py` (396 lines)
**Purpose**: logging module
**Classes**: BrokenStdoutLoggingError, IndentingFormatter, IndentedRenderable, PipConsole, RichPipStreamHandler, BetterRotatingFileHandler, MaxLevelFilter, ExcludeLoggerFilter
**Key functions**: captu

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/utils/misc.py` (771 lines)
**Purpose**: misc module
**Classes**: StreamWrapper, HiddenText, ConfiguredBuildBackendHookCaller
**Key functions**: get_p

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/utils/packaging.py` (44 lines)
**Purpose**: packaging module
**Key functions**: check

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/utils/pylock.py` (283 lines)
**Purpose**: pylock module
**Key functions**: pyloc

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/utils/retry.py` (45 lines)
**Purpose**: retry module
**Key functions**: retry

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/utils/subprocess.py` (248 lines)
**Purpose**: subprocess module
**Key functions**: make_

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/utils/temp_dir.py` (294 lines)
**Purpose**: temp_dir module
**Classes**: TempDirectoryTypeRegistry, _Default, TempDirectory, AdjacentTempDirectory
**Key functions**: globa

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/utils/unpacking.py` (381 lines)
**Purpose**: Utilities related archives.
**Key functions**: curre

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/utils/urls.py` (55 lines)
**Purpose**: urls module
**Key functions**: path_

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/utils/virtualenv.py` (105 lines)
**Purpose**: virtualenv module
**Key functions**: runni

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/utils/wheel.py` (132 lines)
**Purpose**: Support functions for working with wheel files.
**Key functions**: parse

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/vcs/__init__.py` (15 lines)
**Purpose**: __init__ module

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/vcs/bazaar.py` (130 lines)
**Purpose**: bazaar module
**Classes**: Bazaar

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/vcs/git.py` (571 lines)
**Purpose**: git module
**Classes**: Git
**Key functions**: looks

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/vcs/mercurial.py` (186 lines)
**Purpose**: mercurial module
**Classes**: Mercurial

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/vcs/subversion.py` (335 lines)
**Purpose**: subversion module
**Classes**: Subversion

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/vcs/versioncontrol.py` (695 lines)
**Purpose**: Handles all VCS (version control) support
**Classes**: RemoteNotFoundError, RemoteNotValidError, RevOptions, VcsSupport, VersionControl
**Key functions**: is_ur

### `.venv_temp/lib/python3.14/site-packages/pip/_internal/wheel_builder.py` (261 lines)
**Purpose**: Orchestrator for building wheels from InstallRequirements.
**Key functions**: build

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/__init__.py` (117 lines)
**Purpose**: pip._vendor is for vendoring dependencies of pip to prevent needing pip to
depen
**Key functions**: vendo

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/cachecontrol/__init__.py` (32 lines)
**Purpose**: CacheControl import Interface.

Make it easy to import from cachecontrol without

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/cachecontrol/_cmd.py` (70 lines)
**Purpose**: _cmd module
**Key functions**: setup

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/cachecontrol/adapter.py` (167 lines)
**Purpose**: adapter module
**Classes**: CacheControlAdapter

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/cachecontrol/cache.py` (75 lines)
**Purpose**: The cache object API for implementing caches. The default is a thread
safe in-me
**Classes**: BaseCache, DictCache, SeparateBodyBaseCache

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/cachecontrol/caches/__init__.py` (8 lines)
**Purpose**: __init__ module

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/cachecontrol/caches/file_cache.py` (145 lines)
**Purpose**: file_cache module
**Classes**: _FileCacheMixin, FileCache, SeparateBodyFileCache
**Key functions**: url_t

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/cachecontrol/caches/redis_cache.py` (48 lines)
**Purpose**: redis_cache module
**Classes**: RedisCache

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/cachecontrol/controller.py` (511 lines)
**Purpose**: The httplib2 algorithms ported for use with requests.
**Classes**: CacheController
**Key functions**: parse

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/cachecontrol/filewrapper.py` (121 lines)
**Purpose**: filewrapper module
**Classes**: CallbackFileWrapper

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/cachecontrol/heuristics.py` (157 lines)
**Purpose**: heuristics module
**Classes**: BaseHeuristic, OneDayCache, ExpiresAfter, LastModified
**Key functions**: expir

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/cachecontrol/serialize.py` (146 lines)
**Purpose**: serialize module
**Classes**: Serializer

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/cachecontrol/wrapper.py` (43 lines)
**Purpose**: wrapper module
**Key functions**: Cache

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/certifi/__init__.py` (4 lines)
**Purpose**: __init__ module

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/certifi/__main__.py` (12 lines)
**Purpose**: __main__ module

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/certifi/core.py` (83 lines)
**Purpose**: certifi.py
~~~~~~~~~~

This module returns the installation location of cacert.p
**Key functions**: exit_

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/distlib/__init__.py` (33 lines)
**Purpose**: __init__ module
**Classes**: DistlibException, NullHandler

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/distlib/compat.py` (1137 lines)
**Purpose**: compat module
**Classes**: ZipExtFile, ZipFile, CertificateError, Container, ChainMap, OrderedDict, ConvertingDict, ConvertingList, ConvertingTuple, BaseConfigurator

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/distlib/resources.py` (358 lines)
**Purpose**: resources module
**Classes**: ResourceCache, ResourceBase, Resource, ResourceContainer, ResourceFinder, ZipResourceFinder
**Key functions**: regis

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/distlib/scripts.py` (447 lines)
**Purpose**: scripts module
**Classes**: ScriptMaker
**Key functions**: enquo

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/distlib/util.py` (1984 lines)
**Purpose**: util module
**Classes**: cached_property, FileOperator, ExportEntry, Cache, EventMixin, Sequencer, Progress, Transport, ServerProxy, CSVBase, CSVReader, CSVWriter, Configurator, SubprocessMixin, PyPIRCFile, HTTPSConnection, HTTPSHandler, HTTPSOnlyHandler, SafeTransport
**Key functions**: parse

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/distro/__init__.py` (54 lines)
**Purpose**: __init__ module

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/distro/__main__.py` (4 lines)
**Purpose**: __main__ module

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/distro/distro.py` (1403 lines)
**Purpose**: The ``distro`` package (``distro`` stands for Linux Distribution) provides
infor
**Classes**: VersionDict, InfoDict, LinuxDistribution, cached_property
**Key functions**: linux

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/idna/__init__.py` (45 lines)
**Purpose**: __init__ module

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/idna/codec.py` (122 lines)
**Purpose**: codec module
**Classes**: Codec, IncrementalEncoder, IncrementalDecoder, StreamWriter, StreamReader
**Key functions**: searc

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/idna/compat.py` (15 lines)
**Purpose**: compat module
**Key functions**: ToASC

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/idna/core.py` (437 lines)
**Purpose**: core module
**Classes**: IDNAError, IDNABidiError, InvalidCodepoint, InvalidCodepointContext
**Key functions**: valid

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/idna/idnadata.py` (4309 lines)
**Purpose**: idnadata module

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/idna/intranges.py` (57 lines)
**Purpose**: Given a list of integers, made up of (hopefully) a small number of long runs
of 
**Key functions**: intra

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/idna/package_data.py` (1 lines)
**Purpose**: package_data module

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/msgpack/__init__.py` (55 lines)
**Purpose**: __init__ module
**Key functions**: pack,

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/msgpack/exceptions.py` (48 lines)
**Purpose**: exceptions module
**Classes**: UnpackException, BufferFull, OutOfData, FormatError, StackError, ExtraData

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/msgpack/ext.py` (170 lines)
**Purpose**: ext module
**Classes**: ExtType, Timestamp

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/msgpack/fallback.py` (929 lines)
**Purpose**: Fallback pure Python implementation of msgpack
**Classes**: Unpacker, Packer, BytesIO
**Key functions**: unpac

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/packaging/__init__.py` (15 lines)
**Purpose**: __init__ module

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/packaging/_elffile.py` (108 lines)
**Purpose**: ELF file parser.

This provides a class ``ELFFile`` that parses an ELF executabl
**Classes**: ELFInvalid, EIClass, EIData, EMachine, ELFFile

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/packaging/_manylinux.py` (262 lines)
**Purpose**: _manylinux module
**Classes**: _GLibCVersion
**Key functions**: platf

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/packaging/_musllinux.py` (85 lines)
**Purpose**: PEP 656 support.

This module implements logic to detect if the currently runnin
**Classes**: _MuslVersion
**Key functions**: platf

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/packaging/_parser.py` (393 lines)
**Purpose**: Handwritten parser of dependency specifiers.

The docstring for each __parse_* f
**Classes**: Node, Variable, Value, Op, ParsedRequirement
**Key functions**: parse

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/packaging/_structures.py` (33 lines)
**Purpose**: Backward-compatibility shim for unpickling Version objects serialized before
pac
**Classes**: InfinityType, NegativeInfinityType

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/packaging/_tokenizer.py` (193 lines)
**Purpose**: _tokenizer module
**Classes**: Token, ParserSyntaxError, Tokenizer

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/packaging/dependency_groups.py` (302 lines)
**Purpose**: dependency_groups module
**Classes**: DuplicateGroupNames, CyclicDependencyGroup, InvalidDependencyGroupObject, DependencyGroupInclude, DependencyGroupResolver
**Key functions**: resol

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/packaging/direct_url.py` (325 lines)
**Purpose**: direct_url module
**Classes**: _FromMappingProtocol, DirectUrlValidationError, _DirectUrlRequiredKeyError, VcsInfo, ArchiveInfo, DirInfo, DirectUrl

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/packaging/errors.py` (94 lines)
**Purpose**: errors module
**Classes**: _ErrorCollector, ExceptionGroup

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/packaging/licenses/__init__.py` (186 lines)
**Purpose**: __init__ module
**Classes**: InvalidLicenseExpression
**Key functions**: canon

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/packaging/licenses/_spdx.py` (799 lines)
**Purpose**: _spdx module
**Classes**: SPDXLicense, SPDXException

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/packaging/markers.py` (492 lines)
**Purpose**: markers module
**Classes**: InvalidMarker, UndefinedComparison, UndefinedEnvironmentName, Environment, Marker
**Key functions**: defau

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/packaging/metadata.py` (964 lines)
**Purpose**: metadata module
**Classes**: InvalidMetadata, RawMetadata, RFC822Policy, RFC822Message, _Validator, Metadata
**Key functions**: parse

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/packaging/pylock.py` (905 lines)
**Purpose**: pylock module
**Classes**: _FromMappingProtocol, PylockValidationError, _PylockRequiredKeyError, PylockUnsupportedVersionError, PylockSelectError, PackageVcs, PackageDirectory, PackageArchive, PackageSdist, PackageWheel, Package, Pylock
**Key functions**: is_va

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/packaging/requirements.py` (129 lines)
**Purpose**: requirements module
**Classes**: InvalidRequirement, Requirement

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/packaging/specifiers.py` (1943 lines)
**Purpose**: .. testsetup::

    from pip._vendor.packaging.specifiers import Specifier, Spec
**Classes**: _BoundaryKind, _BoundaryVersion, _LowerBound, _UpperBound, InvalidSpecifier, BaseSpecifier, Specifier, SpecifierSet

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/packaging/tags.py` (932 lines)
**Purpose**: tags module
**Classes**: UnsortedTagsError, Tag
**Key functions**: parse

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/packaging/utils.py` (296 lines)
**Purpose**: utils module
**Classes**: InvalidName, InvalidWheelFilename, InvalidSdistFilename
**Key functions**: canon

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/packaging/version.py` (1231 lines)
**Purpose**: .. testsetup::

    from pip._vendor.packaging.version import parse, normalize_p
**Classes**: _VersionReplace, InvalidVersion, _BaseVersion, _Version, Version, _TrimmedRelease
**Key functions**: norma

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/pkg_resources/__init__.py` (3676 lines)
**Purpose**: Package resource API
--------------------

A resource is a logical file containe
**Classes**: _LoaderProtocol, _ZipLoaderModule, PEP440Warning, ResolutionError, VersionConflict, ContextualVersionConflict, DistributionNotFound, UnknownExtra, IMetadataProvider, IResourceProvider, WorkingSet, _ReqExtras, Environment, ExtractionError, ResourceManager, NullProvider, EggProvider, DefaultProvider, EmptyProvider, ZipManifests, MemoizedZipManifests, ZipProvider, FileMetadata, PathMetadata, EggMetadata, NoDists, EntryPoint, Distribution, EggInfoDistribution, DistInfoDistribution, RequirementParseError, Requirement, PkgResourcesDeprecationWarning, manifest_mod
**Key functions**: get_s

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/platformdirs/__init__.py` (631 lines)
**Purpose**: Utilities for determining application-specific dirs.

See <https://github.com/pl
**Key functions**: user_

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/platformdirs/__main__.py` (55 lines)
**Purpose**: Main entry point.
**Key functions**: main

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/platformdirs/android.py` (249 lines)
**Purpose**: Android.
**Classes**: Android

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/platformdirs/api.py` (299 lines)
**Purpose**: Base API.
**Classes**: PlatformDirsABC

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/platformdirs/macos.py` (146 lines)
**Purpose**: macOS.
**Classes**: MacOS

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/platformdirs/unix.py` (272 lines)
**Purpose**: Unix.
**Classes**: Unix

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/platformdirs/version.py` (34 lines)
**Purpose**: version module

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/platformdirs/windows.py` (278 lines)
**Purpose**: Windows.
**Classes**: Windows
**Key functions**: get_w

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/pygments/__init__.py` (82 lines)
**Purpose**: Pygments
~~~~~~~~

Pygments is a syntax highlighting package written in Python.

**Key functions**: lex, 

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/pygments/__main__.py` (17 lines)
**Purpose**: pygments.__main__
~~~~~~~~~~~~~~~~~

Main entry point for ``python -m pygments``

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/pygments/console.py` (70 lines)
**Purpose**: pygments.console
~~~~~~~~~~~~~~~~

Format colored console output.

:copyright: C
**Key functions**: reset

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/pygments/filter.py` (70 lines)
**Purpose**: pygments.filter
~~~~~~~~~~~~~~~

Module that implements the default filter.

:co
**Classes**: Filter, FunctionFilter
**Key functions**: apply

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/pygments/filters/__init__.py` (940 lines)
**Purpose**: pygments.filters
~~~~~~~~~~~~~~~~

Module containing filter lookup functions and
**Classes**: CodeTagFilter, SymbolFilter, KeywordCaseFilter, NameHighlightFilter, ErrorToken, RaiseOnErrorTokenFilter, VisibleWhitespaceFilter, GobbleFilter, TokenMergeFilter
**Key functions**: find_

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/pygments/formatter.py` (129 lines)
**Purpose**: pygments.formatter
~~~~~~~~~~~~~~~~~~

Base formatter class.

:copyright: Copyri
**Classes**: Formatter

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/pygments/formatters/__init__.py` (157 lines)
**Purpose**: pygments.formatters
~~~~~~~~~~~~~~~~~~~

Pygments formatters.

:copyright: Copyr
**Classes**: _automodule
**Key functions**: get_a

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/pygments/formatters/_mapping.py` (23 lines)
**Purpose**: _mapping module

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/pygments/lexer.py` (963 lines)
**Purpose**: pygments.lexer
~~~~~~~~~~~~~~

Base lexer classes.

:copyright: Copyright 2006-2
**Classes**: LexerMeta, Lexer, DelegatingLexer, include, _inherit, combined, _PseudoMatch, _This, default, words, RegexLexerMeta, RegexLexer, LexerContext, ExtendedRegexLexer, ProfilingRegexLexerMeta, ProfilingRegexLexer
**Key functions**: bygro

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/pygments/lexers/__init__.py` (362 lines)
**Purpose**: pygments.lexers
~~~~~~~~~~~~~~~

Pygments lexers.

:copyright: Copyright 2006-20
**Classes**: _automodule
**Key functions**: get_a

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/pygments/lexers/_mapping.py` (602 lines)
**Purpose**: _mapping module

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/pygments/lexers/python.py` (1201 lines)
**Purpose**: pygments.lexers.python
~~~~~~~~~~~~~~~~~~~~~~

Lexers for Python and related lan
**Classes**: PythonLexer, Python2Lexer, _PythonConsoleLexerBase, PythonConsoleLexer, PythonTracebackLexer, Python2TracebackLexer, CythonLexer, DgLexer, NumPyLexer, _ReplaceInnerCode

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/pygments/modeline.py` (43 lines)
**Purpose**: pygments.modeline
~~~~~~~~~~~~~~~~~

A simple modeline parser (based on pymodeli
**Key functions**: get_f

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/pygments/plugin.py` (72 lines)
**Purpose**: pygments.plugin
~~~~~~~~~~~~~~~

Pygments plugin interface.

lexer plugins::

  
**Key functions**: iter_

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/pygments/regexopt.py` (91 lines)
**Purpose**: pygments.regexopt
~~~~~~~~~~~~~~~~~

An algorithm that generates optimized regex
**Key functions**: make_

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/pygments/scanner.py` (104 lines)
**Purpose**: pygments.scanner
~~~~~~~~~~~~~~~~

This library implements a regex based scanner
**Classes**: EndOfText, Scanner

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/pygments/sphinxext.py` (247 lines)
**Purpose**: pygments.sphinxext
~~~~~~~~~~~~~~~~~~

Sphinx extension to generate automatic do
**Classes**: PygmentsDoc
**Key functions**: setup

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/pygments/style.py` (203 lines)
**Purpose**: pygments.style
~~~~~~~~~~~~~~

Basic style object.

:copyright: Copyright 2006-2
**Classes**: StyleMeta, Style

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/pygments/styles/__init__.py` (61 lines)
**Purpose**: pygments.styles
~~~~~~~~~~~~~~~

Contains built-in styles.

:copyright: Copyrigh
**Key functions**: get_s

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/pygments/styles/_mapping.py` (54 lines)
**Purpose**: _mapping module

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/pygments/token.py` (214 lines)
**Purpose**: pygments.token
~~~~~~~~~~~~~~

Basic token types and the standard tokens.

:copy
**Classes**: _TokenType
**Key functions**: is_to

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/pygments/unistring.py` (153 lines)
**Purpose**: pygments.unistring
~~~~~~~~~~~~~~~~~~

Strings of all Unicode characters of a ce
**Key functions**: combi

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/pygments/util.py` (324 lines)
**Purpose**: pygments.util
~~~~~~~~~~~~~

Utility functions.

:copyright: Copyright 2006-2025
**Classes**: ClassNotFound, OptionError, Future, UnclosingTextIOWrapper
**Key functions**: get_c

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/pyproject_hooks/__init__.py` (31 lines)
**Purpose**: Wrappers to call pyproject.toml-based build backend hooks.

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/pyproject_hooks/_impl.py` (410 lines)
**Purpose**: _impl module
**Classes**: BackendUnavailable, HookMissing, UnsupportedOperation, BuildBackendHookCaller, SubprocessRunner
**Key functions**: write

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/pyproject_hooks/_in_process/__init__.py` (21 lines)
**Purpose**: This is a subpackage because the directory is on sys.path for _in_process.py

Th

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/pyproject_hooks/_in_process/_in_process.py` (389 lines)
**Purpose**: This is invoked in a subprocess to call the build backend hooks.

It expects:
- 
**Classes**: BackendUnavailable, HookMissing, _BackendPathFinder, _DummyException, GotUnsupportedOperation
**Key functions**: write

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/requests/__init__.py` (178 lines)
**Purpose**: Requests HTTP Library
~~~~~~~~~~~~~~~~~~~~~

Requests is an HTTP library, writte
**Key functions**: check

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/requests/__version__.py` (14 lines)
**Purpose**: __version__ module

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/requests/_internal_utils.py` (51 lines)
**Purpose**: requests._internal_utils
~~~~~~~~~~~~~~

Provides utility functions that are con
**Key functions**: to_na

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/requests/adapters.py` (697 lines)
**Purpose**: requests.adapters
~~~~~~~~~~~~~~~~~

This module contains the transport adapters
**Classes**: BaseAdapter, HTTPAdapter

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/requests/api.py` (157 lines)
**Purpose**: requests.api
~~~~~~~~~~~~

This module implements the Requests API.

:copyright:
**Key functions**: reque

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/requests/auth.py` (314 lines)
**Purpose**: requests.auth
~~~~~~~~~~~~~

This module contains the authentication handlers fo
**Classes**: AuthBase, HTTPBasicAuth, HTTPProxyAuth, HTTPDigestAuth

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/requests/certs.py` (18 lines)
**Purpose**: requests.certs
~~~~~~~~~~~~~~

This module returns the preferred default CA cert

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/requests/compat.py` (90 lines)
**Purpose**: requests.compat
~~~~~~~~~~~~~~~

This module previously handled import compatibi

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/requests/cookies.py` (561 lines)
**Purpose**: requests.cookies
~~~~~~~~~~~~~~~~

Compatibility code to be able to use `http.co
**Classes**: MockRequest, MockResponse, CookieConflictError, RequestsCookieJar
**Key functions**: extra

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/requests/exceptions.py` (152 lines)
**Purpose**: requests.exceptions
~~~~~~~~~~~~~~~~~~~

This module contains the set of Request
**Classes**: RequestException, InvalidJSONError, JSONDecodeError, HTTPError, ConnectionError, ProxyError, SSLError, Timeout, ConnectTimeout, ReadTimeout, URLRequired, TooManyRedirects, MissingSchema, InvalidSchema, InvalidURL, InvalidHeader, InvalidProxyURL, ChunkedEncodingError, ContentDecodingError, StreamConsumedError, RetryError, UnrewindableBodyError, RequestsWarning, FileModeWarning, RequestsDependencyWarning

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/requests/help.py` (124 lines)
**Purpose**: Module containing bug report helper(s).
**Key functions**: info,

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/requests/hooks.py` (34 lines)
**Purpose**: requests.hooks
~~~~~~~~~~~~~~

This module provides the capabilities for the Req
**Key functions**: defau

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/requests/models.py` (1041 lines)
**Purpose**: requests.models
~~~~~~~~~~~~~~~

This module contains the primary objects that p
**Classes**: RequestEncodingMixin, RequestHooksMixin, Request, PreparedRequest, Response

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/requests/packages.py` (25 lines)
**Purpose**: packages module

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/requests/sessions.py` (834 lines)
**Purpose**: requests.sessions
~~~~~~~~~~~~~~~~~

This module provides a Session object to ma
**Classes**: SessionRedirectMixin, Session
**Key functions**: merge

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/requests/status_codes.py` (128 lines)
**Purpose**: The ``codes`` object defines a mapping from common names for HTTP statuses
to th

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/requests/structures.py` (99 lines)
**Purpose**: requests.structures
~~~~~~~~~~~~~~~~~~~

Data structures that power Requests.
**Classes**: CaseInsensitiveDict, LookupDict

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/requests/utils.py` (1083 lines)
**Purpose**: requests.utils
~~~~~~~~~~~~~~

This module provides utility functions that are u
**Key functions**: dict_

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/resolvelib/__init__.py` (27 lines)
**Purpose**: __init__ module

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/resolvelib/providers.py` (196 lines)
**Purpose**: providers module
**Classes**: AbstractProvider, Preference

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/resolvelib/reporters.py` (55 lines)
**Purpose**: reporters module
**Classes**: BaseReporter

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/resolvelib/resolvers/__init__.py` (27 lines)
**Purpose**: __init__ module

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/resolvelib/resolvers/abstract.py` (47 lines)
**Purpose**: abstract module
**Classes**: AbstractResolver, Result

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/resolvelib/resolvers/criterion.py` (48 lines)
**Purpose**: criterion module
**Classes**: Criterion

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/resolvelib/resolvers/exceptions.py` (57 lines)
**Purpose**: exceptions module
**Classes**: ResolverException, RequirementsConflicted, InconsistentCandidate, ResolutionError, ResolutionImpossible, ResolutionTooDeep

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/resolvelib/resolvers/resolution.py` (627 lines)
**Purpose**: resolution module
**Classes**: Resolution, Resolver

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/resolvelib/structs.py` (209 lines)
**Purpose**: structs module
**Classes**: DirectedGraph, IteratorMapping, _FactoryIterableView, _SequenceIterableView, RequirementInformation, State
**Key functions**: build

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/rich/__init__.py` (177 lines)
**Purpose**: Rich text and beautiful formatting in the terminal.
**Key functions**: get_c

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/rich/__main__.py` (245 lines)
**Purpose**: __main__ module
**Classes**: ColorBox
**Key functions**: make_

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/rich/_cell_widths.py` (454 lines)
**Purpose**: _cell_widths module

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/rich/_emoji_codes.py` (3610 lines)
**Purpose**: _emoji_codes module

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/rich/_emoji_replace.py` (32 lines)
**Purpose**: _emoji_replace module

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/rich/_export_format.py` (76 lines)
**Purpose**: _export_format module

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/rich/_extension.py` (10 lines)
**Purpose**: _extension module
**Key functions**: load_

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/rich/_fileno.py` (24 lines)
**Purpose**: _fileno module
**Key functions**: get_f

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/rich/_inspect.py` (268 lines)
**Purpose**: _inspect module
**Classes**: Inspect
**Key functions**: get_o

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/rich/_log_render.py` (94 lines)
**Purpose**: _log_render module
**Classes**: LogRender

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/rich/_loop.py` (43 lines)
**Purpose**: _loop module
**Key functions**: loop_

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/rich/_null_file.py` (69 lines)
**Purpose**: _null_file module
**Classes**: NullFile

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/rich/_palettes.py` (309 lines)
**Purpose**: _palettes module

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/rich/_pick.py` (17 lines)
**Purpose**: _pick module
**Key functions**: pick_

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/rich/_ratio.py` (153 lines)
**Purpose**: _ratio module
**Classes**: Edge, E
**Key functions**: ratio

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/rich/_spinners.py` (482 lines)
**Purpose**: Spinners are from:
* cli-spinners:
    MIT License
    Copyright (c) Sindre Sorh

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/rich/_stack.py` (16 lines)
**Purpose**: _stack module
**Classes**: Stack

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/rich/_timer.py` (19 lines)
**Purpose**: Timer context manager, only used in debug.
**Key functions**: timer

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/rich/_win32_console.py` (661 lines)
**Purpose**: Light wrapper around the Win32 Console API - this module should only be imported
**Classes**: LegacyWindowsError, WindowsCoordinates, CONSOLE_SCREEN_BUFFER_INFO, CONSOLE_CURSOR_INFO, LegacyWindowsTerm
**Key functions**: GetSt

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/rich/_windows.py` (71 lines)
**Purpose**: _windows module
**Classes**: WindowsConsoleFeatures

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/rich/_windows_renderer.py` (56 lines)
**Purpose**: _windows_renderer module
**Key functions**: legac

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/rich/_wrap.py` (93 lines)
**Purpose**: _wrap module
**Key functions**: words

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/rich/abc.py` (33 lines)
**Purpose**: abc module
**Classes**: RichRenderable, Foo

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/rich/align.py` (306 lines)
**Purpose**: align module
**Classes**: Align, VerticalCenter

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/rich/ansi.py` (241 lines)
**Purpose**: ansi module
**Classes**: _AnsiToken, AnsiDecoder

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/rich/bar.py` (93 lines)
**Purpose**: bar module
**Classes**: Bar

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/rich/box.py` (474 lines)
**Purpose**: box module
**Classes**: Box

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/rich/cells.py` (174 lines)
**Purpose**: cells module
**Key functions**: cache

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/rich/color.py` (621 lines)
**Purpose**: color module
**Classes**: ColorSystem, ColorType, ColorParseError, Color
**Key functions**: parse

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/rich/color_triplet.py` (38 lines)
**Purpose**: color_triplet module
**Classes**: ColorTriplet

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/rich/columns.py` (187 lines)
**Purpose**: columns module
**Classes**: Columns

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/rich/console.py` (2680 lines)
**Purpose**: console module
**Classes**: NoChange, ConsoleDimensions, ConsoleOptions, RichCast, ConsoleRenderable, CaptureError, NewLine, ScreenUpdate, Capture, ThemeContext, PagerContext, ScreenContext, Group, ConsoleThreadLocals, RenderHook, Console
**Key functions**: group

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/rich/constrain.py` (37 lines)
**Purpose**: constrain module
**Classes**: Constrain

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/rich/containers.py` (167 lines)
**Purpose**: containers module
**Classes**: Renderables, Lines

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/rich/control.py` (219 lines)
**Purpose**: control module
**Classes**: Control
**Key functions**: strip

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/rich/default_styles.py` (193 lines)
**Purpose**: default_styles module

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/rich/diagnose.py` (39 lines)
**Purpose**: diagnose module
**Key functions**: repor

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/rich/emoji.py` (91 lines)
**Purpose**: emoji module
**Classes**: NoEmoji, Emoji

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/rich/errors.py` (34 lines)
**Purpose**: errors module
**Classes**: ConsoleError, StyleError, StyleSyntaxError, MissingStyle, StyleStackError, NotRenderableError, MarkupError, LiveError, NoAltScreen

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/rich/file_proxy.py` (57 lines)
**Purpose**: file_proxy module
**Classes**: FileProxy

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/rich/filesize.py` (88 lines)
**Purpose**: Functions for reporting filesizes. Borrowed from https://github.com/PyFilesystem
**Key functions**: pick_

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/rich/highlighter.py` (232 lines)
**Purpose**: highlighter module
**Classes**: Highlighter, NullHighlighter, RegexHighlighter, ReprHighlighter, JSONHighlighter, ISO8601Highlighter

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/rich/json.py` (139 lines)
**Purpose**: json module
**Classes**: JSON

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/rich/jupyter.py` (101 lines)
**Purpose**: jupyter module
**Classes**: JupyterRenderable, JupyterMixin
**Key functions**: displ

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/rich/layout.py` (442 lines)
**Purpose**: layout module
**Classes**: LayoutRender, LayoutError, NoSplitter, _Placeholder, Splitter, RowSplitter, ColumnSplitter, Layout

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/rich/live.py` (400 lines)
**Purpose**: Live trading executor via ccxt market orders
**Classes**: _RefreshThread, Live

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/rich/live_render.py` (106 lines)
**Purpose**: live_render module
**Classes**: LiveRender

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/rich/logging.py` (297 lines)
**Purpose**: logging module
**Classes**: RichHandler

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/rich/markup.py` (251 lines)
**Purpose**: markup module
**Classes**: Tag
**Key functions**: escap

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/rich/measure.py` (151 lines)
**Purpose**: measure module
**Classes**: Measurement
**Key functions**: measu

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/rich/padding.py` (141 lines)
**Purpose**: padding module
**Classes**: Padding

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/rich/pager.py` (34 lines)
**Purpose**: pager module
**Classes**: Pager, SystemPager

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/rich/palette.py` (100 lines)
**Purpose**: palette module
**Classes**: Palette, ColorBox

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/rich/panel.py` (317 lines)
**Purpose**: panel module
**Classes**: Panel

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/rich/pretty.py` (1016 lines)
**Purpose**: pretty module
**Classes**: Pretty, Node, _Line, BrokenRepr, StockKeepingUnit, Thing, RichFormatter
**Key functions**: insta

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/rich/progress.py` (1715 lines)
**Purpose**: progress module
**Classes**: _TrackThread, _Reader, _ReadContext, ProgressColumn, RenderableColumn, SpinnerColumn, TextColumn, BarColumn, TimeElapsedColumn, TaskProgressColumn, TimeRemainingColumn, FileSizeColumn, TotalFileSizeColumn, MofNCompleteColumn, DownloadColumn, TransferSpeedColumn, ProgressSample, Task, Progress
**Key functions**: track

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/rich/progress_bar.py` (223 lines)
**Purpose**: progress_bar module
**Classes**: ProgressBar

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/rich/prompt.py` (400 lines)
**Purpose**: prompt module
**Classes**: PromptError, InvalidResponse, PromptBase, Prompt, IntPrompt, FloatPrompt, Confirm

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/rich/protocol.py` (42 lines)
**Purpose**: protocol module
**Key functions**: is_re

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/rich/region.py` (10 lines)
**Purpose**: region module
**Classes**: Region

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/rich/repr.py` (149 lines)
**Purpose**: repr module
**Classes**: ReprError, Foo
**Key functions**: auto,

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/rich/rule.py` (130 lines)
**Purpose**: rule module
**Classes**: Rule

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/rich/scope.py` (86 lines)
**Purpose**: scope module
**Key functions**: rende

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/rich/screen.py` (54 lines)
**Purpose**: screen module
**Classes**: Screen

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/rich/segment.py` (752 lines)
**Purpose**: segment module
**Classes**: ControlType, Segment, Segments, SegmentLines

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/rich/spinner.py` (132 lines)
**Purpose**: spinner module
**Classes**: Spinner

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/rich/status.py` (131 lines)
**Purpose**: status module
**Classes**: Status

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/rich/style.py` (792 lines)
**Purpose**: style module
**Classes**: _Bit, Style, StyleStack

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/rich/styled.py` (42 lines)
**Purpose**: styled module
**Classes**: Styled

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/rich/syntax.py` (985 lines)
**Purpose**: syntax module
**Classes**: SyntaxTheme, PygmentsSyntaxTheme, ANSISyntaxTheme, _SyntaxHighlightRange, PaddingProperty, Syntax

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/rich/table.py` (1006 lines)
**Purpose**: table module
**Classes**: Column, Row, _Cell, Table

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/rich/terminal_theme.py` (153 lines)
**Purpose**: terminal_theme module
**Classes**: TerminalTheme

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/rich/text.py` (1361 lines)
**Purpose**: text module
**Classes**: Span, Text

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/rich/theme.py` (115 lines)
**Purpose**: theme module
**Classes**: Theme, ThemeStackError, ThemeStack

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/rich/themes.py` (5 lines)
**Purpose**: themes module

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/rich/traceback.py` (899 lines)
**Purpose**: traceback module
**Classes**: Frame, _SyntaxError, Stack, Trace, PathHighlighter, Traceback
**Key functions**: insta

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/rich/tree.py` (257 lines)
**Purpose**: tree module
**Classes**: Tree

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/tomli/__init__.py` (8 lines)
**Purpose**: __init__ module

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/tomli/_parser.py` (788 lines)
**Purpose**: _parser module
**Classes**: DEPRECATED_DEFAULT, TOMLDecodeError, Flags, NestedDict, Output
**Key functions**: load,

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/tomli/_re.py` (115 lines)
**Purpose**: _re module
**Key functions**: match

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/tomli/_types.py` (10 lines)
**Purpose**: _types module

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/tomli_w/__init__.py` (4 lines)
**Purpose**: __init__ module

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/tomli_w/_writer.py` (229 lines)
**Purpose**: _writer module
**Classes**: Context
**Key functions**: dump,

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/truststore/__init__.py` (36 lines)
**Purpose**: Verify certificates using native system trust stores

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/truststore/_api.py` (341 lines)
**Purpose**: _api module
**Classes**: SSLContext, TruststoreSSLObject
**Key functions**: injec

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/truststore/_macos.py` (571 lines)
**Purpose**: _macos module
**Classes**: CFConst

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/truststore/_openssl.py` (68 lines)
**Purpose**: _openssl module

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/truststore/_ssl_constants.py` (31 lines)
**Purpose**: _ssl_constants module

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/truststore/_windows.py` (567 lines)
**Purpose**: _windows module
**Classes**: CERT_CONTEXT, CERT_ENHKEY_USAGE, CERT_USAGE_MATCH, CERT_CHAIN_PARA, CERT_TRUST_STATUS, CERT_CHAIN_ELEMENT, CERT_SIMPLE_CHAIN, CERT_CHAIN_CONTEXT, SSL_EXTRA_CERT_CHAIN_POLICY_PARA, CERT_CHAIN_POLICY_PARA, CERT_CHAIN_POLICY_STATUS, CERT_CHAIN_ENGINE_CONFIG

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/urllib3/__init__.py` (211 lines)
**Purpose**: Python HTTP library with thread-safe connection pooling, file post support, user
**Key functions**: add_s

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/urllib3/_base_connection.py` (165 lines)
**Purpose**: _base_connection module
**Classes**: ProxyConfig, _ResponseOptions, BaseHTTPConnection, BaseHTTPSConnection

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/urllib3/_collections.py` (487 lines)
**Purpose**: _collections module
**Classes**: _Sentinel, RecentlyUsedContainer, HTTPHeaderDictItemView, HTTPHeaderDict, HasGettableStringKeys
**Key functions**: ensur

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/urllib3/_request_methods.py` (278 lines)
**Purpose**: _request_methods module
**Classes**: RequestMethods

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/urllib3/_version.py` (34 lines)
**Purpose**: _version module

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/urllib3/connection.py` (1099 lines)
**Purpose**: connection module
**Classes**: HTTPConnection, HTTPSConnection, _WrappedAndVerifiedSocket, DummyConnection, BaseSSLError

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/urllib3/connectionpool.py` (1178 lines)
**Purpose**: connectionpool module
**Classes**: ConnectionPool, HTTPConnectionPool, HTTPSConnectionPool
**Key functions**: conne

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/urllib3/contrib/__init__.py` (0 lines)
**Purpose**: __init__ module

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/urllib3/contrib/emscripten/__init__.py` (17 lines)
**Purpose**: __init__ module
**Key functions**: injec

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/urllib3/contrib/emscripten/connection.py` (260 lines)
**Purpose**: connection module
**Classes**: EmscriptenHTTPConnection, EmscriptenHTTPSConnection

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/urllib3/contrib/emscripten/fetch.py` (726 lines)
**Purpose**: Support for streaming http requests in emscripten.

A few caveats -

If your bro
**Classes**: _RequestError, _StreamingError, _TimeoutError, _ReadStream, _StreamingFetcher, _JSPIReadStream
**Key functions**: is_in

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/urllib3/contrib/emscripten/request.py` (22 lines)
**Purpose**: request module
**Classes**: EmscriptenRequest

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/urllib3/contrib/emscripten/response.py` (277 lines)
**Purpose**: response module
**Classes**: EmscriptenResponse, EmscriptenHttpResponseWrapper

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/urllib3/contrib/pyopenssl.py` (564 lines)
**Purpose**: Module for using pyOpenSSL as a TLS backend. This module was relevant before
the
**Classes**: WrappedSocket, PyOpenSSLContext, UnsupportedExtension
**Key functions**: injec

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/urllib3/contrib/socks.py` (228 lines)
**Purpose**: This module contains provisional support for SOCKS proxies from within
urllib3. 
**Classes**: _TYPE_SOCKS_OPTIONS, SOCKSConnection, SOCKSHTTPSConnection, SOCKSHTTPConnectionPool, SOCKSHTTPSConnectionPool, SOCKSProxyManager

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/urllib3/exceptions.py` (335 lines)
**Purpose**: exceptions module
**Classes**: HTTPError, HTTPWarning, PoolError, RequestError, SSLError, ProxyError, DecodeError, ProtocolError, MaxRetryError, HostChangedError, TimeoutStateError, TimeoutError, ReadTimeoutError, ConnectTimeoutError, NewConnectionError, NameResolutionError, EmptyPoolError, FullPoolError, ClosedPoolError, LocationValueError, LocationParseError, URLSchemeUnknown, ResponseError, SecurityWarning, InsecureRequestWarning, NotOpenSSLWarning, SystemTimeWarning, InsecurePlatformWarning, DependencyWarning, ResponseNotChunked, BodyNotHttplibCompatible, IncompleteRead, InvalidChunkLength, InvalidHeader, ProxySchemeUnknown, ProxySchemeUnsupported, HeaderParsingError, UnrewindableBodyError

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/urllib3/fields.py` (341 lines)
**Purpose**: fields module
**Classes**: RequestField
**Key functions**: guess

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/urllib3/filepost.py` (89 lines)
**Purpose**: filepost module
**Key functions**: choos

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/urllib3/http2/__init__.py` (53 lines)
**Purpose**: __init__ module
**Key functions**: injec

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/urllib3/http2/connection.py` (356 lines)
**Purpose**: connection module
**Classes**: _LockedObject, HTTP2Connection, HTTP2Response

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/urllib3/http2/probe.py` (87 lines)
**Purpose**: probe module
**Classes**: _HTTP2ProbeCache

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/urllib3/poolmanager.py` (651 lines)
**Purpose**: poolmanager module
**Classes**: PoolKey, PoolManager, ProxyManager
**Key functions**: proxy

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/urllib3/response.py` (1474 lines)
**Purpose**: response module
**Classes**: ContentDecoder, DeflateDecoder, GzipDecoderState, GzipDecoder, MultiDecoder, BytesQueueBuffer, BaseHTTPResponse, HTTPResponse, BrotliDecoder, ZstdDecoder

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/urllib3/util/__init__.py` (42 lines)
**Purpose**: __init__ module

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/urllib3/util/connection.py` (137 lines)
**Purpose**: connection module
**Key functions**: is_co

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/urllib3/util/proxy.py` (43 lines)
**Purpose**: proxy module
**Key functions**: conne

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/urllib3/util/request.py` (254 lines)
**Purpose**: request module
**Classes**: _TYPE_FAILEDTELL, ChunksAndContentLength
**Key functions**: make_

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/urllib3/util/response.py` (101 lines)
**Purpose**: response module
**Key functions**: is_fp

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/urllib3/util/retry.py` (549 lines)
**Purpose**: retry module
**Classes**: RequestHistory, Retry

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/urllib3/util/ssl_.py` (527 lines)
**Purpose**: ssl_ module
**Classes**: _TYPE_PEER_CERT_RET_DICT
**Key functions**: asser

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/urllib3/util/ssl_match_hostname.py` (159 lines)
**Purpose**: The match_hostname() function from Python 3.5, essential when using SSL.
**Classes**: CertificateError
**Key functions**: match

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/urllib3/util/ssltransport.py` (271 lines)
**Purpose**: ssltransport module
**Classes**: SSLTransport

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/urllib3/util/timeout.py` (275 lines)
**Purpose**: timeout module
**Classes**: _TYPE_DEFAULT, Timeout

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/urllib3/util/url.py` (469 lines)
**Purpose**: url module
**Classes**: Url
**Key functions**: parse

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/urllib3/util/util.py` (42 lines)
**Purpose**: util module
**Key functions**: to_by

### `.venv_temp/lib/python3.14/site-packages/pip/_vendor/urllib3/util/wait.py` (124 lines)
**Purpose**: wait module
**Key functions**: selec

### `.venv_temp/lib/python3.14/site-packages/setuptools/__init__.py` (256 lines)
**Purpose**: Extensions to the 'distutils' for large or complex distributions
**Classes**: Command, sic, MinimalDistribution
**Key functions**: setup

### `.venv_temp/lib/python3.14/site-packages/setuptools/_core_metadata.py` (337 lines)
**Purpose**: Handling of Core Metadata for Python packages (including reading and writing).


**Key functions**: get_m

### `.venv_temp/lib/python3.14/site-packages/setuptools/_discovery.py` (33 lines)
**Purpose**: _discovery module
**Key functions**: extra

### `.venv_temp/lib/python3.14/site-packages/setuptools/_distutils/__init__.py` (14 lines)
**Purpose**: __init__ module

### `.venv_temp/lib/python3.14/site-packages/setuptools/_distutils/_log.py` (3 lines)
**Purpose**: _log module

### `.venv_temp/lib/python3.14/site-packages/setuptools/_distutils/_macos_compat.py` (12 lines)
**Purpose**: _macos_compat module
**Key functions**: bypas

### `.venv_temp/lib/python3.14/site-packages/setuptools/_distutils/_modified.py` (95 lines)
**Purpose**: Timestamp comparison of files and groups of files.
**Key functions**: newer

### `.venv_temp/lib/python3.14/site-packages/setuptools/_distutils/_msvccompiler.py` (16 lines)
**Purpose**: _msvccompiler module

### `.venv_temp/lib/python3.14/site-packages/setuptools/_distutils/archive_util.py` (284 lines)
**Purpose**: distutils.archive_util

Utility functions for creating archive files (tarballs, 
**Key functions**: make_

### `.venv_temp/lib/python3.14/site-packages/setuptools/_distutils/ccompiler.py` (26 lines)
**Purpose**: ccompiler module

### `.venv_temp/lib/python3.14/site-packages/setuptools/_distutils/cmd.py` (535 lines)
**Purpose**: distutils.cmd

Provides the Command class, the base class for the command classe
**Classes**: Command

### `.venv_temp/lib/python3.14/site-packages/setuptools/_distutils/command/__init__.py` (23 lines)
**Purpose**: distutils.command

Package containing implementation of all the standard Distuti

### `.venv_temp/lib/python3.14/site-packages/setuptools/_distutils/command/_framework_compat.py` (54 lines)
**Purpose**: Backward compatibility for homebrew builds on macOS.
**Key functions**: enabl

### `.venv_temp/lib/python3.14/site-packages/setuptools/_distutils/command/bdist.py` (167 lines)
**Purpose**: distutils.command.bdist

Implements the Distutils 'bdist' command (create a buil
**Classes**: ListCompat, bdist
**Key functions**: show_

### `.venv_temp/lib/python3.14/site-packages/setuptools/_distutils/command/bdist_dumb.py` (141 lines)
**Purpose**: distutils.command.bdist_dumb

Implements the Distutils 'bdist_dumb' command (cre
**Classes**: bdist_dumb

### `.venv_temp/lib/python3.14/site-packages/setuptools/_distutils/command/bdist_rpm.py` (597 lines)
**Purpose**: distutils.command.bdist_rpm

Implements the Distutils 'bdist_rpm' command (creat
**Classes**: bdist_rpm

### `.venv_temp/lib/python3.14/site-packages/setuptools/_distutils/command/build.py` (156 lines)
**Purpose**: distutils.command.build

Implements the Distutils 'build' command.
**Classes**: build

### `.venv_temp/lib/python3.14/site-packages/setuptools/_distutils/command/build_clib.py` (199 lines)
**Purpose**: distutils.command.build_clib

Implements the Distutils 'build_clib' command, to 
**Classes**: build_clib

### `.venv_temp/lib/python3.14/site-packages/setuptools/_distutils/command/build_ext.py` (811 lines)
**Purpose**: distutils.command.build_ext

Implements the Distutils 'build_ext' command, for b
**Classes**: build_ext

### `.venv_temp/lib/python3.14/site-packages/setuptools/_distutils/command/build_py.py` (404 lines)
**Purpose**: distutils.command.build_py

Implements the Distutils 'build_py' command.
**Classes**: build_py

### `.venv_temp/lib/python3.14/site-packages/setuptools/_distutils/command/build_scripts.py` (150 lines)
**Purpose**: distutils.command.build_scripts

Implements the Distutils 'build_scripts' comman
**Classes**: build_scripts

### `.venv_temp/lib/python3.14/site-packages/setuptools/_distutils/command/check.py` (152 lines)
**Purpose**: distutils.command.check

Implements the Distutils 'check' command.
**Classes**: check, SilentReporter

### `.venv_temp/lib/python3.14/site-packages/setuptools/_distutils/command/clean.py` (76 lines)
**Purpose**: distutils.command.clean

Implements the Distutils 'clean' command.
**Classes**: clean

### `.venv_temp/lib/python3.14/site-packages/setuptools/_distutils/command/config.py` (348 lines)
**Purpose**: distutils.command.config

Implements the Distutils 'config' command, a (mostly) 
**Classes**: config
**Key functions**: dump_

### `.venv_temp/lib/python3.14/site-packages/setuptools/_distutils/command/install.py` (805 lines)
**Purpose**: distutils.command.install

Implements the Distutils 'install' command.
**Classes**: install

### `.venv_temp/lib/python3.14/site-packages/setuptools/_distutils/command/install_data.py` (94 lines)
**Purpose**: distutils.command.install_data

Implements the Distutils 'install_data' command,
**Classes**: install_data

### `.venv_temp/lib/python3.14/site-packages/setuptools/_distutils/command/install_egg_info.py` (90 lines)
**Purpose**: distutils.command.install_egg_info

Implements the Distutils 'install_egg_info' 
**Classes**: install_egg_info
**Key functions**: safe_

### `.venv_temp/lib/python3.14/site-packages/setuptools/_distutils/command/install_headers.py` (46 lines)
**Purpose**: distutils.command.install_headers

Implements the Distutils 'install_headers' co
**Classes**: install_headers

### `.venv_temp/lib/python3.14/site-packages/setuptools/_distutils/command/install_lib.py` (236 lines)
**Purpose**: distutils.command.install_lib

Implements the Distutils 'install_lib' command
(i
**Classes**: install_lib

### `.venv_temp/lib/python3.14/site-packages/setuptools/_distutils/command/install_scripts.py` (59 lines)
**Purpose**: distutils.command.install_scripts

Implements the Distutils 'install_scripts' co
**Classes**: install_scripts

### `.venv_temp/lib/python3.14/site-packages/setuptools/_distutils/command/sdist.py` (521 lines)
**Purpose**: distutils.command.sdist

Implements the Distutils 'sdist' command (create a sour
**Classes**: sdist
**Key functions**: show_

### `.venv_temp/lib/python3.14/site-packages/setuptools/_distutils/compat/__init__.py` (18 lines)
**Purpose**: __init__ module
**Key functions**: conso

### `.venv_temp/lib/python3.14/site-packages/setuptools/_distutils/compat/numpy.py` (2 lines)
**Purpose**: numpy module

### `.venv_temp/lib/python3.14/site-packages/setuptools/_distutils/compat/py39.py` (66 lines)
**Purpose**: py39 module
**Classes**: UnequalIterablesError
**Key functions**: add_e

### `.venv_temp/lib/python3.14/site-packages/setuptools/_distutils/compilers/C/base.py` (1386 lines)
**Purpose**: distutils.ccompiler

Contains Compiler, an abstract base class that defines the 
**Classes**: Compiler
**Key functions**: get_d

### `.venv_temp/lib/python3.14/site-packages/setuptools/_distutils/compilers/C/cygwin.py` (340 lines)
**Purpose**: distutils.cygwinccompiler

Provides the CygwinCCompiler class, a subclass of Uni
**Classes**: Compiler, MinGW32Compiler
**Key functions**: get_m

### `.venv_temp/lib/python3.14/site-packages/setuptools/_distutils/compilers/C/errors.py` (24 lines)
**Purpose**: errors module
**Classes**: Error, PreprocessError, CompileError, LibError, LinkError, UnknownFileType

### `.venv_temp/lib/python3.14/site-packages/setuptools/_distutils/compilers/C/msvc.py` (614 lines)
**Purpose**: distutils._msvccompiler

Contains MSVCCompiler, an implementation of the abstrac
**Classes**: Compiler

### `.venv_temp/lib/python3.14/site-packages/setuptools/_distutils/compilers/C/tests/test_base.py` (83 lines)
**Purpose**: test_base module
**Key functions**: c_fil

### `.venv_temp/lib/python3.14/site-packages/setuptools/_distutils/compilers/C/tests/test_cygwin.py` (76 lines)
**Purpose**: Tests for distutils.cygwinccompiler.
**Classes**: TestCygwinCCompiler
**Key functions**: stuff

### `.venv_temp/lib/python3.14/site-packages/setuptools/_distutils/compilers/C/tests/test_mingw.py` (48 lines)
**Purpose**: test_mingw module
**Classes**: TestMinGW32Compiler

### `.venv_temp/lib/python3.14/site-packages/setuptools/_distutils/compilers/C/tests/test_msvc.py` (136 lines)
**Purpose**: test_msvc module
**Classes**: Testmsvccompiler, CheckThread, TestSpawn

### `.venv_temp/lib/python3.14/site-packages/setuptools/_distutils/compilers/C/tests/test_unix.py` (413 lines)
**Purpose**: Tests for distutils.unixccompiler.
**Classes**: TestUnixCCompiler, CompilerWrapper
**Key functions**: save_

### `.venv_temp/lib/python3.14/site-packages/setuptools/_distutils/compilers/C/unix.py` (422 lines)
**Purpose**: distutils.unixccompiler

Contains the UnixCCompiler class, a subclass of CCompil
**Classes**: Compiler

### `.venv_temp/lib/python3.14/site-packages/setuptools/_distutils/compilers/C/zos.py` (230 lines)
**Purpose**: distutils.zosccompiler

Contains the selection of the c & c++ compilers on z/OS.
**Classes**: Compiler

### `.venv_temp/lib/python3.14/site-packages/setuptools/_distutils/core.py` (289 lines)
**Purpose**: distutils.core

The only module that needs to be imported to use the Distutils; 
**Key functions**: gen_u

### `.venv_temp/lib/python3.14/site-packages/setuptools/_distutils/cygwinccompiler.py` (31 lines)
**Purpose**: cygwinccompiler module

### `.venv_temp/lib/python3.14/site-packages/setuptools/_distutils/debug.py` (5 lines)
**Purpose**: debug module

### `.venv_temp/lib/python3.14/site-packages/setuptools/_distutils/dep_util.py` (14 lines)
**Purpose**: dep_util module

### `.venv_temp/lib/python3.14/site-packages/setuptools/_distutils/dir_util.py` (232 lines)
**Purpose**: distutils.dir_util

Utility functions for manipulating directories and directory
**Classes**: SkipRepeatAbsolutePaths
**Key functions**: mkpat

### `.venv_temp/lib/python3.14/site-packages/setuptools/_distutils/dist.py` (1384 lines)
**Purpose**: distutils.dist

Provides the Distribution class, which represents the module dis
**Classes**: Distribution, DistributionMetadata
**Key functions**: fix_h

### `.venv_temp/lib/python3.14/site-packages/setuptools/_distutils/errors.py` (108 lines)
**Purpose**: Exceptions used by the Distutils modules.

Distutils modules may raise these or 
**Classes**: DistutilsError, DistutilsModuleError, DistutilsClassError, DistutilsGetoptError, DistutilsArgError, DistutilsFileError, DistutilsOptionError, DistutilsSetupError, DistutilsPlatformError, DistutilsExecError, DistutilsInternalError, DistutilsTemplateError, DistutilsByteCompileError

### `.venv_temp/lib/python3.14/site-packages/setuptools/_distutils/extension.py` (258 lines)
**Purpose**: distutils.extension

Provides the Extension class, used to describe C/C++ extens
**Classes**: Extension
**Key functions**: read_

### `.venv_temp/lib/python3.14/site-packages/setuptools/_distutils/fancy_getopt.py` (471 lines)
**Purpose**: distutils.fancy_getopt

Wrapper around the standard getopt module that provides 
**Classes**: FancyGetopt, OptionDummy
**Key functions**: fancy

### `.venv_temp/lib/python3.14/site-packages/setuptools/_distutils/file_util.py` (228 lines)
**Purpose**: distutils.file_util

Utility functions for operating on single files.
**Key functions**: copy_

### `.venv_temp/lib/python3.14/site-packages/setuptools/_distutils/filelist.py` (431 lines)
**Purpose**: distutils.filelist

Provides the FileList class, used for poking about the files
**Classes**: FileList, _UniqueDirs
**Key functions**: finda

### `.venv_temp/lib/python3.14/site-packages/setuptools/_distutils/log.py` (56 lines)
**Purpose**: A simple log mechanism styled after PEP 282.

Retained for compatibility and sho
**Classes**: Log
**Key functions**: set_t

### `.venv_temp/lib/python3.14/site-packages/setuptools/_distutils/spawn.py` (130 lines)
**Purpose**: distutils.spawn

Provides the 'spawn()' function, a front-end to various platfor
**Key functions**: spawn

### `.venv_temp/lib/python3.14/site-packages/setuptools/_distutils/sysconfig.py` (598 lines)
**Purpose**: Provide access to Python's configuration information.  The specific
configuratio
**Key functions**: get_p

### `.venv_temp/lib/python3.14/site-packages/setuptools/_distutils/tests/__init__.py` (42 lines)
**Purpose**: Test suite for distutils.

Tests for the command classes in the distutils.comman
**Key functions**: missi

### `.venv_temp/lib/python3.14/site-packages/setuptools/_distutils/tests/compat/__init__.py` (0 lines)
**Purpose**: __init__ module

### `.venv_temp/lib/python3.14/site-packages/setuptools/_distutils/tests/compat/py39.py` (40 lines)
**Purpose**: py39 module

### `.venv_temp/lib/python3.14/site-packages/setuptools/_distutils/tests/support.py` (134 lines)
**Purpose**: Support code for distutils test cases.
**Classes**: TempdirManager, DummyCommand
**Key functions**: copy_

### `.venv_temp/lib/python3.14/site-packages/setuptools/_distutils/tests/test_archive_util.py` (342 lines)
**Purpose**: Tests for distutils.archive_util.
**Classes**: ArchiveUtilTestCase
**Key functions**: can_f

### `.venv_temp/lib/python3.14/site-packages/setuptools/_distutils/tests/test_bdist.py` (47 lines)
**Purpose**: Tests for distutils.command.bdist.
**Classes**: TestBuild

### `.venv_temp/lib/python3.14/site-packages/setuptools/_distutils/tests/test_bdist_dumb.py` (78 lines)
**Purpose**: Tests for distutils.command.bdist_dumb.
**Classes**: TestBuildDumb

### `.venv_temp/lib/python3.14/site-packages/setuptools/_distutils/tests/test_bdist_rpm.py` (127 lines)
**Purpose**: Tests for distutils.command.bdist_rpm.
**Classes**: TestBuildRpm
**Key functions**: sys_e

### `.venv_temp/lib/python3.14/site-packages/setuptools/_distutils/tests/test_build.py` (49 lines)
**Purpose**: Tests for distutils.command.build.
**Classes**: TestBuild

### `.venv_temp/lib/python3.14/site-packages/setuptools/_distutils/tests/test_build_clib.py` (134 lines)
**Purpose**: Tests for distutils.command.build_clib.
**Classes**: TestBuildCLib, FakeCompiler

### `.venv_temp/lib/python3.14/site-packages/setuptools/_distutils/tests/test_build_ext.py` (628 lines)
**Purpose**: test_build_ext module
**Classes**: TestBuildExt, TestParallelBuildExt
**Key functions**: user_

### `.venv_temp/lib/python3.14/site-packages/setuptools/_distutils/tests/test_build_py.py` (196 lines)
**Purpose**: Tests for distutils.command.build_py.
**Classes**: TestBuildPy

### `.venv_temp/lib/python3.14/site-packages/setuptools/_distutils/tests/test_build_scripts.py` (96 lines)
**Purpose**: Tests for distutils.command.build_scripts.
**Classes**: TestBuildScripts

### `.venv_temp/lib/python3.14/site-packages/setuptools/_distutils/tests/test_check.py` (194 lines)
**Purpose**: Tests for distutils.command.check.
**Classes**: TestCheck

### `.venv_temp/lib/python3.14/site-packages/setuptools/_distutils/tests/test_clean.py` (45 lines)
**Purpose**: Tests for distutils.command.clean.
**Classes**: TestClean

### `.venv_temp/lib/python3.14/site-packages/setuptools/_distutils/tests/test_cmd.py` (107 lines)
**Purpose**: Tests for distutils.cmd.
**Classes**: MyCmd, TestCommand
**Key functions**: cmd

### `.venv_temp/lib/python3.14/site-packages/setuptools/_distutils/tests/test_config_cmd.py` (87 lines)
**Purpose**: Tests for distutils.command.config.
**Classes**: TestConfig
**Key functions**: info_

### `.venv_temp/lib/python3.14/site-packages/setuptools/_distutils/tests/test_core.py` (130 lines)
**Purpose**: Tests for distutils.core.
**Classes**: TestCore
**Key functions**: save_

### `.venv_temp/lib/python3.14/site-packages/setuptools/_distutils/tests/test_dir_util.py` (139 lines)
**Purpose**: Tests for distutils.dir_util.
**Classes**: TestDirUtil, FailPath
**Key functions**: stuff

### `.venv_temp/lib/python3.14/site-packages/setuptools/_distutils/tests/test_dist.py` (552 lines)
**Purpose**: Tests for distutils.dist.
**Classes**: test_dist, TestDistribution, TestDistributionBehavior, TestMetadata
**Key functions**: clear

### `.venv_temp/lib/python3.14/site-packages/setuptools/_distutils/tests/test_extension.py` (117 lines)
**Purpose**: Tests for distutils.extension.
**Classes**: TestExtension

### `.venv_temp/lib/python3.14/site-packages/setuptools/_distutils/tests/test_file_util.py` (95 lines)
**Purpose**: Tests for distutils.file_util.
**Classes**: TestFileUtil
**Key functions**: stuff

### `.venv_temp/lib/python3.14/site-packages/setuptools/_distutils/tests/test_filelist.py` (336 lines)
**Purpose**: Tests for distutils.filelist.
**Classes**: TestFileList, TestFindAll
**Key functions**: make_

### `.venv_temp/lib/python3.14/site-packages/setuptools/_distutils/tests/test_install.py` (245 lines)
**Purpose**: Tests for distutils.command.install.
**Classes**: TestInstall

### `.venv_temp/lib/python3.14/site-packages/setuptools/_distutils/tests/test_install_data.py` (74 lines)
**Purpose**: Tests for distutils.command.install_data.
**Classes**: TestInstallData

### `.venv_temp/lib/python3.14/site-packages/setuptools/_distutils/tests/test_install_headers.py` (33 lines)
**Purpose**: Tests for distutils.command.install_headers.
**Classes**: TestInstallHeaders

### `.venv_temp/lib/python3.14/site-packages/setuptools/_distutils/tests/test_install_lib.py` (110 lines)
**Purpose**: Tests for distutils.command.install_data.
**Classes**: TestInstallLib

### `.venv_temp/lib/python3.14/site-packages/setuptools/_distutils/tests/test_install_scripts.py` (52 lines)
**Purpose**: Tests for distutils.command.install_scripts.
**Classes**: TestInstallScripts

### `.venv_temp/lib/python3.14/site-packages/setuptools/_distutils/tests/test_log.py` (12 lines)
**Purpose**: Tests for distutils.log
**Classes**: TestLog

### `.venv_temp/lib/python3.14/site-packages/setuptools/_distutils/tests/test_modified.py` (126 lines)
**Purpose**: Tests for distutils._modified.
**Classes**: TestDepUtil
**Key functions**: group

### `.venv_temp/lib/python3.14/site-packages/setuptools/_distutils/tests/test_sdist.py` (470 lines)
**Purpose**: Tests for distutils.command.sdist.
**Classes**: TestSDist
**Key functions**: proje

### `.venv_temp/lib/python3.14/site-packages/setuptools/_distutils/tests/test_spawn.py` (141 lines)
**Purpose**: Tests for distutils.spawn.
**Classes**: TestSpawn

### `.venv_temp/lib/python3.14/site-packages/setuptools/_distutils/tests/test_sysconfig.py` (319 lines)
**Purpose**: Tests for distutils.sysconfig.
**Classes**: TestSysconfig, compiler

### `.venv_temp/lib/python3.14/site-packages/setuptools/_distutils/tests/test_text_file.py` (127 lines)
**Purpose**: Tests for distutils.text_file.
**Classes**: TestTextFile

### `.venv_temp/lib/python3.14/site-packages/setuptools/_distutils/tests/test_util.py` (243 lines)
**Purpose**: Tests for distutils.util.
**Classes**: TestUtil
**Key functions**: envir

### `.venv_temp/lib/python3.14/site-packages/setuptools/_distutils/tests/test_version.py` (80 lines)
**Purpose**: Tests for distutils.version.
**Classes**: TestVersion
**Key functions**: suppr

### `.venv_temp/lib/python3.14/site-packages/setuptools/_distutils/tests/test_versionpredicate.py` (0 lines)
**Purpose**: test_versionpredicate module

### `.venv_temp/lib/python3.14/site-packages/setuptools/_distutils/tests/unix_compat.py` (17 lines)
**Purpose**: unix_compat module

### `.venv_temp/lib/python3.14/site-packages/setuptools/_distutils/text_file.py` (286 lines)
**Purpose**: text_file

provides the TextFile class, which gives an interface to text files
t
**Classes**: TextFile

### `.venv_temp/lib/python3.14/site-packages/setuptools/_distutils/unixccompiler.py` (9 lines)
**Purpose**: unixccompiler module

### `.venv_temp/lib/python3.14/site-packages/setuptools/_distutils/util.py` (506 lines)
**Purpose**: distutils.util

Miscellaneous utility functions -- anything that doesn't fit int
**Key functions**: get_h

### `.venv_temp/lib/python3.14/site-packages/setuptools/_distutils/version.py` (348 lines)
**Purpose**: Provides classes to represent module version numbers (one class for
each style o
**Classes**: Version, StrictVersion, LooseVersion
**Key functions**: suppr

### `.venv_temp/lib/python3.14/site-packages/setuptools/_distutils/versionpredicate.py` (175 lines)
**Purpose**: Module for parsing and testing package version predicate strings.
**Classes**: VersionPredicate
**Key functions**: split

### `.venv_temp/lib/python3.14/site-packages/setuptools/_distutils/zosccompiler.py` (3 lines)
**Purpose**: zosccompiler module

### `.venv_temp/lib/python3.14/site-packages/setuptools/_entry_points.py` (94 lines)
**Purpose**: _entry_points module
**Key functions**: ensur

### `.venv_temp/lib/python3.14/site-packages/setuptools/_imp.py` (87 lines)
**Purpose**: Re-implementation of find_module and get_frozen_object
from the deprecated imp m
**Key functions**: find_

### `.venv_temp/lib/python3.14/site-packages/setuptools/_importlib.py` (9 lines)
**Purpose**: _importlib module

### `.venv_temp/lib/python3.14/site-packages/setuptools/_itertools.py` (23 lines)
**Purpose**: _itertools module
**Key functions**: ensur

### `.venv_temp/lib/python3.14/site-packages/setuptools/_normalization.py` (180 lines)
**Purpose**: Helpers for normalization as expected in wheel/sdist/module file names
and core 
**Key functions**: safe_

### `.venv_temp/lib/python3.14/site-packages/setuptools/_path.py` (93 lines)
**Purpose**: _path module
**Key functions**: ensur

### `.venv_temp/lib/python3.14/site-packages/setuptools/_reqs.py` (42 lines)
**Purpose**: _reqs module
**Key functions**: parse

### `.venv_temp/lib/python3.14/site-packages/setuptools/_scripts.py` (361 lines)
**Purpose**: _scripts module
**Classes**: _SplitArgs, CommandSpec, WindowsCommandSpec, ScriptWriter, WindowsScriptWriter, WindowsExecutableLauncherWriter
**Key functions**: get_w

### `.venv_temp/lib/python3.14/site-packages/setuptools/_shutil.py` (59 lines)
**Purpose**: Convenience layer on top of stdlib's shutil and os
**Key functions**: attem

### `.venv_temp/lib/python3.14/site-packages/setuptools/_static.py` (188 lines)
**Purpose**: _static module
**Classes**: Static, Str, Tuple, List, Dict, SpecifierSet
**Key functions**: noop,

### `.venv_temp/lib/python3.14/site-packages/setuptools/_vendor/autocommand/__init__.py` (27 lines)
**Purpose**: __init__ module

### `.venv_temp/lib/python3.14/site-packages/setuptools/_vendor/autocommand/autoasync.py` (142 lines)
**Purpose**: autoasync module
**Key functions**: autoa

### `.venv_temp/lib/python3.14/site-packages/setuptools/_vendor/autocommand/autocommand.py` (70 lines)
**Purpose**: autocommand module
**Key functions**: autoc

### `.venv_temp/lib/python3.14/site-packages/setuptools/_vendor/autocommand/automain.py` (59 lines)
**Purpose**: automain module
**Classes**: AutomainRequiresModuleError
**Key functions**: autom

### `.venv_temp/lib/python3.14/site-packages/setuptools/_vendor/autocommand/autoparse.py` (333 lines)
**Purpose**: autoparse module
**Classes**: AnnotationError, PositionalArgError, KWArgError, DocstringError, TooManySplitsError
**Key functions**: make_

### `.venv_temp/lib/python3.14/site-packages/setuptools/_vendor/autocommand/errors.py` (23 lines)
**Purpose**: errors module
**Classes**: AutocommandError

### `.venv_temp/lib/python3.14/site-packages/setuptools/_vendor/backports/__init__.py` (1 lines)
**Purpose**: __init__ module

### `.venv_temp/lib/python3.14/site-packages/setuptools/_vendor/backports/tarfile/__init__.py` (2937 lines)
**Purpose**: Read from and write to tar format archives.
**Classes**: TarError, ExtractError, ReadError, CompressionError, StreamError, HeaderError, EmptyHeaderError, TruncatedHeaderError, EOFHeaderError, InvalidHeaderError, SubsequentHeaderError, _LowLevelFile, _Stream, _StreamProxy, _FileInFile, ExFileObject, FilterError, AbsolutePathError, OutsideDestinationError, SpecialFileError, AbsoluteLinkError, LinkOutsideDestinationError, TarInfo, TarFile
**Key functions**: stn, 

### `.venv_temp/lib/python3.14/site-packages/setuptools/_vendor/backports/tarfile/__main__.py` (5 lines)
**Purpose**: __main__ module

### `.venv_temp/lib/python3.14/site-packages/setuptools/_vendor/backports/tarfile/compat/__init__.py` (0 lines)
**Purpose**: __init__ module

### `.venv_temp/lib/python3.14/site-packages/setuptools/_vendor/backports/tarfile/compat/py38.py` (24 lines)
**Purpose**: py38 module

### `.venv_temp/lib/python3.14/site-packages/setuptools/_vendor/importlib_metadata/__init__.py` (1191 lines)
**Purpose**: APIs exposing metadata from third-party Python packages.

This codebase is share
**Classes**: PackageNotFoundError, Sectioned, _EntryPointMatch, EntryPoint, EntryPoints, PackagePath, FileHash, Distribution, DistributionFinder, FastPath, Lookup, Prepared, MetadataPathFinder, PathDistribution, Context
**Key functions**: distr

### `.venv_temp/lib/python3.14/site-packages/setuptools/_vendor/importlib_metadata/_adapters.py` (136 lines)
**Purpose**: _adapters module
**Classes**: RawPolicy, Message

### `.venv_temp/lib/python3.14/site-packages/setuptools/_vendor/importlib_metadata/_collections.py` (34 lines)
**Purpose**: _collections module
**Classes**: FreezableDefaultDict, Pair

### `.venv_temp/lib/python3.14/site-packages/setuptools/_vendor/importlib_metadata/_compat.py` (56 lines)
**Purpose**: _compat module
**Classes**: NullFinder
**Key functions**: insta

### `.venv_temp/lib/python3.14/site-packages/setuptools/_vendor/importlib_metadata/_functools.py` (135 lines)
**Purpose**: _functools module
**Key functions**: metho

### `.venv_temp/lib/python3.14/site-packages/setuptools/_vendor/importlib_metadata/_itertools.py` (171 lines)
**Purpose**: _itertools module
**Classes**: bucket
**Key functions**: uniqu

### `.venv_temp/lib/python3.14/site-packages/setuptools/_vendor/importlib_metadata/_meta.py` (71 lines)
**Purpose**: _meta module
**Classes**: PackageMetadata, SimplePath

### `.venv_temp/lib/python3.14/site-packages/setuptools/_vendor/importlib_metadata/_text.py` (99 lines)
**Purpose**: _text module
**Classes**: FoldedCase

### `.venv_temp/lib/python3.14/site-packages/setuptools/_vendor/importlib_metadata/_typing.py` (15 lines)
**Purpose**: _typing module

### `.venv_temp/lib/python3.14/site-packages/setuptools/_vendor/importlib_metadata/compat/__init__.py` (0 lines)
**Purpose**: __init__ module

### `.venv_temp/lib/python3.14/site-packages/setuptools/_vendor/importlib_metadata/compat/py311.py` (22 lines)
**Purpose**: py311 module
**Key functions**: wrap

### `.venv_temp/lib/python3.14/site-packages/setuptools/_vendor/importlib_metadata/compat/py39.py` (42 lines)
**Purpose**: Compatibility layer with Python 3.8/3.9
**Key functions**: norma

### `.venv_temp/lib/python3.14/site-packages/setuptools/_vendor/importlib_metadata/diagnose.py` (21 lines)
**Purpose**: diagnose module
**Key functions**: inspe

### `.venv_temp/lib/python3.14/site-packages/setuptools/_vendor/jaraco/context/__init__.py` (367 lines)
**Purpose**: __init__ module
**Classes**: ExceptionTrap, suppress, on_interrupt
**Key functions**: pushd

### `.venv_temp/lib/python3.14/site-packages/setuptools/_vendor/jaraco/functools/__init__.py` (722 lines)
**Purpose**: __init__ module
**Classes**: Throttler
**Key functions**: compo

### `.venv_temp/lib/python3.14/site-packages/setuptools/_vendor/jaraco/text/__init__.py` (647 lines)
**Purpose**: __init__ module
**Classes**: FoldedCase, Splitter, WordSet, SeparatedValues, Stripper
**Key functions**: subst

### `.venv_temp/lib/python3.14/site-packages/setuptools/_vendor/jaraco/text/layouts.py` (25 lines)
**Purpose**: layouts module
**Key functions**: trans

### `.venv_temp/lib/python3.14/site-packages/setuptools/_vendor/jaraco/text/show-newlines.py` (32 lines)
**Purpose**: show-newlines module
**Key functions**: repor

### `.venv_temp/lib/python3.14/site-packages/setuptools/_vendor/jaraco/text/strip-prefix.py` (21 lines)
**Purpose**: strip-prefix module
**Key functions**: strip

### `.venv_temp/lib/python3.14/site-packages/setuptools/_vendor/jaraco/text/to-dvorak.py` (5 lines)
**Purpose**: to-dvorak module

### `.venv_temp/lib/python3.14/site-packages/setuptools/_vendor/jaraco/text/to-qwerty.py` (5 lines)
**Purpose**: to-qwerty module

### `.venv_temp/lib/python3.14/site-packages/setuptools/_vendor/more_itertools/__init__.py` (6 lines)
**Purpose**: More routines for operating on iterables, beyond itertools

### `.venv_temp/lib/python3.14/site-packages/setuptools/_vendor/more_itertools/more.py` (5303 lines)
**Purpose**: more module
**Classes**: peekable, bucket, numeric_range, islice_extended, SequenceView, seekable, run_length, time_limited, AbortThread, callback_iter, countable
**Key functions**: chunk

### `.venv_temp/lib/python3.14/site-packages/setuptools/_vendor/more_itertools/recipes.py` (1471 lines)
**Purpose**: Imported from the recipes section of the itertools documentation.

All functions
**Classes**: UnequalIterablesError
**Key functions**: take,

### `.venv_temp/lib/python3.14/site-packages/setuptools/_vendor/packaging/__init__.py` (15 lines)
**Purpose**: __init__ module

### `.venv_temp/lib/python3.14/site-packages/setuptools/_vendor/packaging/_elffile.py` (108 lines)
**Purpose**: ELF file parser.

This provides a class ``ELFFile`` that parses an ELF executabl
**Classes**: ELFInvalid, EIClass, EIData, EMachine, ELFFile

### `.venv_temp/lib/python3.14/site-packages/setuptools/_vendor/packaging/_manylinux.py` (262 lines)
**Purpose**: _manylinux module
**Classes**: _GLibCVersion
**Key functions**: platf

### `.venv_temp/lib/python3.14/site-packages/setuptools/_vendor/packaging/_musllinux.py` (85 lines)
**Purpose**: PEP 656 support.

This module implements logic to detect if the currently runnin
**Classes**: _MuslVersion
**Key functions**: platf

### `.venv_temp/lib/python3.14/site-packages/setuptools/_vendor/packaging/_parser.py` (365 lines)
**Purpose**: Handwritten parser of dependency specifiers.

The docstring for each __parse_* f
**Classes**: Node, Variable, Value, Op, ParsedRequirement
**Key functions**: parse

### `.venv_temp/lib/python3.14/site-packages/setuptools/_vendor/packaging/_structures.py` (69 lines)
**Purpose**: _structures module
**Classes**: InfinityType, NegativeInfinityType

### `.venv_temp/lib/python3.14/site-packages/setuptools/_vendor/packaging/_tokenizer.py` (193 lines)
**Purpose**: _tokenizer module
**Classes**: Token, ParserSyntaxError, Tokenizer

### `.venv_temp/lib/python3.14/site-packages/setuptools/_vendor/packaging/licenses/__init__.py` (147 lines)
**Purpose**: __init__ module
**Classes**: InvalidLicenseExpression
**Key functions**: canon

### `.venv_temp/lib/python3.14/site-packages/setuptools/_vendor/packaging/licenses/_spdx.py` (799 lines)
**Purpose**: _spdx module
**Classes**: SPDXLicense, SPDXException

### `.venv_temp/lib/python3.14/site-packages/setuptools/_vendor/packaging/markers.py` (388 lines)
**Purpose**: markers module
**Classes**: InvalidMarker, UndefinedComparison, UndefinedEnvironmentName, Environment, Marker
**Key functions**: forma

### `.venv_temp/lib/python3.14/site-packages/setuptools/_vendor/packaging/metadata.py` (978 lines)
**Purpose**: metadata module
**Classes**: InvalidMetadata, RawMetadata, RFC822Policy, RFC822Message, _Validator, Metadata, ExceptionGroup
**Key functions**: parse

### `.venv_temp/lib/python3.14/site-packages/setuptools/_vendor/packaging/pylock.py` (635 lines)
**Purpose**: pylock module
**Classes**: _FromMappingProtocol, PylockValidationError, _PylockRequiredKeyError, PylockUnsupportedVersionError, PackageVcs, PackageDirectory, PackageArchive, PackageSdist, PackageWheel, Package, Pylock
**Key functions**: is_va

### `.venv_temp/lib/python3.14/site-packages/setuptools/_vendor/packaging/requirements.py` (86 lines)
**Purpose**: requirements module
**Classes**: InvalidRequirement, Requirement

### `.venv_temp/lib/python3.14/site-packages/setuptools/_vendor/packaging/specifiers.py` (1068 lines)
**Purpose**: .. testsetup::

    from packaging.specifiers import Specifier, SpecifierSet, In
**Classes**: InvalidSpecifier, BaseSpecifier, Specifier, SpecifierSet

### `.venv_temp/lib/python3.14/site-packages/setuptools/_vendor/packaging/tags.py` (651 lines)
**Purpose**: tags module
**Classes**: Tag
**Key functions**: parse

### `.venv_temp/lib/python3.14/site-packages/setuptools/_vendor/packaging/utils.py` (158 lines)
**Purpose**: utils module
**Classes**: InvalidName, InvalidWheelFilename, InvalidSdistFilename
**Key functions**: canon

### `.venv_temp/lib/python3.14/site-packages/setuptools/_vendor/packaging/version.py` (792 lines)
**Purpose**: .. testsetup::

    from packaging.version import parse, Version
**Classes**: _VersionReplace, InvalidVersion, _BaseVersion, _Version, Version, _TrimmedRelease
**Key functions**: parse

### `.venv_temp/lib/python3.14/site-packages/setuptools/_vendor/platformdirs/__init__.py` (631 lines)
**Purpose**: Utilities for determining application-specific dirs.

See <https://github.com/pl
**Key functions**: user_

### `.venv_temp/lib/python3.14/site-packages/setuptools/_vendor/platformdirs/__main__.py` (55 lines)
**Purpose**: Main entry point.
**Key functions**: main

### `.venv_temp/lib/python3.14/site-packages/setuptools/_vendor/platformdirs/android.py` (249 lines)
**Purpose**: Android.
**Classes**: Android

### `.venv_temp/lib/python3.14/site-packages/setuptools/_vendor/platformdirs/api.py` (299 lines)
**Purpose**: Base API.
**Classes**: PlatformDirsABC

### `.venv_temp/lib/python3.14/site-packages/setuptools/_vendor/platformdirs/macos.py` (146 lines)
**Purpose**: macOS.
**Classes**: MacOS

### `.venv_temp/lib/python3.14/site-packages/setuptools/_vendor/platformdirs/unix.py` (272 lines)
**Purpose**: Unix.
**Classes**: Unix

### `.venv_temp/lib/python3.14/site-packages/setuptools/_vendor/platformdirs/version.py` (34 lines)
**Purpose**: version module

### `.venv_temp/lib/python3.14/site-packages/setuptools/_vendor/platformdirs/windows.py` (272 lines)
**Purpose**: Windows.
**Classes**: Windows
**Key functions**: get_w

### `.venv_temp/lib/python3.14/site-packages/setuptools/_vendor/tomli/__init__.py` (8 lines)
**Purpose**: __init__ module

### `.venv_temp/lib/python3.14/site-packages/setuptools/_vendor/tomli/_parser.py` (782 lines)
**Purpose**: _parser module
**Classes**: DEPRECATED_DEFAULT, TOMLDecodeError, Flags, NestedDict, Output
**Key functions**: load,

### `.venv_temp/lib/python3.14/site-packages/setuptools/_vendor/tomli/_re.py` (119 lines)
**Purpose**: _re module
**Key functions**: match

### `.venv_temp/lib/python3.14/site-packages/setuptools/_vendor/tomli/_types.py` (10 lines)
**Purpose**: _types module

### `.venv_temp/lib/python3.14/site-packages/setuptools/_vendor/wheel/__init__.py` (3 lines)
**Purpose**: __init__ module

### `.venv_temp/lib/python3.14/site-packages/setuptools/_vendor/wheel/__main__.py` (25 lines)
**Purpose**: Wheel command line tool (enables the ``python -m wheel`` syntax)
**Key functions**: main

### `.venv_temp/lib/python3.14/site-packages/setuptools/_vendor/wheel/_bdist_wheel.py` (616 lines)
**Purpose**: Create a wheel (.whl) distribution.

A wheel is a built archive format.
**Classes**: bdist_wheel
**Key functions**: safe_

### `.venv_temp/lib/python3.14/site-packages/setuptools/_vendor/wheel/_commands/__init__.py` (153 lines)
**Purpose**: Wheel command-line utility.
**Key functions**: unpac

### `.venv_temp/lib/python3.14/site-packages/setuptools/_vendor/wheel/_commands/convert.py` (337 lines)
**Purpose**: convert module
**Classes**: ConvertSource, EggFileSource, EggDirectorySource, WininstFileSource
**Key functions**: conve

### `.venv_temp/lib/python3.14/site-packages/setuptools/_vendor/wheel/_commands/pack.py` (84 lines)
**Purpose**: pack module
**Key functions**: pack,

### `.venv_temp/lib/python3.14/site-packages/setuptools/_vendor/wheel/_commands/tags.py` (140 lines)
**Purpose**: tags module
**Key functions**: tags

### `.venv_temp/lib/python3.14/site-packages/setuptools/_vendor/wheel/_commands/unpack.py` (30 lines)
**Purpose**: unpack module
**Key functions**: unpac

### `.venv_temp/lib/python3.14/site-packages/setuptools/_vendor/wheel/_metadata.py` (184 lines)
**Purpose**: Tools for converting old- to new-style metadata.
**Key functions**: yield

### `.venv_temp/lib/python3.14/site-packages/setuptools/_vendor/wheel/_setuptools_logging.py` (26 lines)
**Purpose**: _setuptools_logging module
**Key functions**: confi

### `.venv_temp/lib/python3.14/site-packages/setuptools/_vendor/wheel/bdist_wheel.py` (26 lines)
**Purpose**: bdist_wheel module

### `.venv_temp/lib/python3.14/site-packages/setuptools/_vendor/wheel/macosx_libfile.py` (486 lines)
**Purpose**: IMPORTANT: DO NOT IMPORT THIS MODULE DIRECTLY.
THIS IS ONLY KEPT IN PLACE FOR BA
**Classes**: SegmentBase, MachHeader, MachHeader, FatHeader, VersionMinCommand, FatArch, FatArch, VersionBuild
**Key functions**: swap3

### `.venv_temp/lib/python3.14/site-packages/setuptools/_vendor/wheel/metadata.py` (17 lines)
**Purpose**: metadata module

### `.venv_temp/lib/python3.14/site-packages/setuptools/_vendor/wheel/wheelfile.py` (241 lines)
**Purpose**: wheelfile module
**Classes**: WheelError, WheelFile
**Key functions**: urlsa

### `.venv_temp/lib/python3.14/site-packages/setuptools/_vendor/zipp/__init__.py` (456 lines)
**Purpose**: A Path-like interface for zipfiles.

This codebase is shared between zipfile.Pat
**Classes**: InitializedState, CompleteDirs, FastLookup, Path

### `.venv_temp/lib/python3.14/site-packages/setuptools/_vendor/zipp/_functools.py` (20 lines)
**Purpose**: _functools module
**Key functions**: save_

### `.venv_temp/lib/python3.14/site-packages/setuptools/_vendor/zipp/compat/__init__.py` (0 lines)
**Purpose**: __init__ module

### `.venv_temp/lib/python3.14/site-packages/setuptools/_vendor/zipp/compat/overlay.py` (37 lines)
**Purpose**: Expose zipp.Path as .zipfile.Path.

Includes everything else in ``zipfile`` to m
**Classes**: HashableNamespace

### `.venv_temp/lib/python3.14/site-packages/setuptools/_vendor/zipp/compat/py310.py` (13 lines)
**Purpose**: py310 module

### `.venv_temp/lib/python3.14/site-packages/setuptools/_vendor/zipp/compat/py313.py` (34 lines)
**Purpose**: py313 module
**Key functions**: ident

### `.venv_temp/lib/python3.14/site-packages/setuptools/_vendor/zipp/glob.py` (116 lines)
**Purpose**: glob module
**Classes**: Translator
**Key functions**: separ

### `.venv_temp/lib/python3.14/site-packages/setuptools/archive_util.py` (219 lines)
**Purpose**: Utilities for extracting common archive formats
**Classes**: UnrecognizedFormat
**Key functions**: defau

### `.venv_temp/lib/python3.14/site-packages/setuptools/build_meta.py` (556 lines)
**Purpose**: A PEP 517 interface to setuptools

Previously, when a user or a command line too
**Classes**: SetupRequirementsError, Distribution, _ConfigSettingsTranslator, _BuildMetaBackend, _BuildMetaLegacyBackend, _IncompatibleBdistWheel
**Key functions**: no_in

### `.venv_temp/lib/python3.14/site-packages/setuptools/command/__init__.py` (21 lines)
**Purpose**: __init__ module

### `.venv_temp/lib/python3.14/site-packages/setuptools/command/_requirestxt.py` (131 lines)
**Purpose**: Helper code used to generate ``requires.txt`` files in the egg-info directory.


**Key functions**: write

### `.venv_temp/lib/python3.14/site-packages/setuptools/command/alias.py` (77 lines)
**Purpose**: alias module
**Classes**: alias
**Key functions**: shquo

### `.venv_temp/lib/python3.14/site-packages/setuptools/command/bdist_egg.py` (471 lines)
**Purpose**: setuptools.command.bdist_egg

Build .egg distributions
**Classes**: bdist_egg
**Key functions**: strip

### `.venv_temp/lib/python3.14/site-packages/setuptools/command/bdist_rpm.py` (42 lines)
**Purpose**: bdist_rpm module
**Classes**: bdist_rpm

### `.venv_temp/lib/python3.14/site-packages/setuptools/command/bdist_wheel.py` (603 lines)
**Purpose**: Create a wheel (.whl) distribution.

A wheel is a built archive format.
**Classes**: bdist_wheel
**Key functions**: safe_

### `.venv_temp/lib/python3.14/site-packages/setuptools/command/build.py` (135 lines)
**Purpose**: build module
**Classes**: build, SubCommand

### `.venv_temp/lib/python3.14/site-packages/setuptools/command/build_clib.py` (103 lines)
**Purpose**: build_clib module
**Classes**: build_clib

### `.venv_temp/lib/python3.14/site-packages/setuptools/command/build_ext.py` (470 lines)
**Purpose**: build_ext module
**Classes**: build_ext
**Key functions**: get_a

### `.venv_temp/lib/python3.14/site-packages/setuptools/command/build_py.py` (403 lines)
**Purpose**: build_py module
**Classes**: build_py, _IncludePackageDataAbuse, _Warning
**Key functions**: make_

### `.venv_temp/lib/python3.14/site-packages/setuptools/command/develop.py` (58 lines)
**Purpose**: develop module
**Classes**: develop, DevelopDeprecationWarning

### `.venv_temp/lib/python3.14/site-packages/setuptools/command/dist_info.py` (103 lines)
**Purpose**: Create a dist_info directory
As defined in the wheel specification
**Classes**: dist_info

### `.venv_temp/lib/python3.14/site-packages/setuptools/command/easy_install.py` (30 lines)
**Purpose**: easy_install module
**Classes**: easy_install

### `.venv_temp/lib/python3.14/site-packages/setuptools/command/editable_wheel.py` (914 lines)
**Purpose**: Create a wheel that, when installed, will make the source package 'editable'
(ad
**Classes**: _EditableMode, editable_wheel, EditableStrategy, _StaticPth, _LinkTree, _TopLevelFinder, _NamespaceInstaller, LinksNotSupported

### `.venv_temp/lib/python3.14/site-packages/setuptools/command/egg_info.py` (716 lines)
**Purpose**: setuptools.command.egg_info

Create a distribution's .egg-info directory and con
**Classes**: InfoCommon, egg_info, FileList, manifest_maker, EggInfoDeprecationWarning
**Key functions**: trans

### `.venv_temp/lib/python3.14/site-packages/setuptools/command/install.py` (131 lines)
**Purpose**: install module
**Classes**: install

### `.venv_temp/lib/python3.14/site-packages/setuptools/command/install_egg_info.py` (57 lines)
**Purpose**: install_egg_info module
**Classes**: install_egg_info

### `.venv_temp/lib/python3.14/site-packages/setuptools/command/install_lib.py` (137 lines)
**Purpose**: install_lib module
**Classes**: install_lib

### `.venv_temp/lib/python3.14/site-packages/setuptools/command/install_scripts.py` (66 lines)
**Purpose**: install_scripts module
**Classes**: install_scripts

### `.venv_temp/lib/python3.14/site-packages/setuptools/command/rotate.py` (64 lines)
**Purpose**: rotate module
**Classes**: rotate

### `.venv_temp/lib/python3.14/site-packages/setuptools/command/saveopts.py` (21 lines)
**Purpose**: saveopts module
**Classes**: saveopts

### `.venv_temp/lib/python3.14/site-packages/setuptools/command/sdist.py` (218 lines)
**Purpose**: sdist module
**Classes**: sdist, NoValue
**Key functions**: walk_

### `.venv_temp/lib/python3.14/site-packages/setuptools/command/setopt.py` (139 lines)
**Purpose**: setopt module
**Classes**: option_base, setopt
**Key functions**: confi

### `.venv_temp/lib/python3.14/site-packages/setuptools/command/test.py` (47 lines)
**Purpose**: test module
**Classes**: _test

### `.venv_temp/lib/python3.14/site-packages/setuptools/compat/__init__.py` (0 lines)
**Purpose**: __init__ module

### `.venv_temp/lib/python3.14/site-packages/setuptools/compat/py310.py` (20 lines)
**Purpose**: py310 module

### `.venv_temp/lib/python3.14/site-packages/setuptools/compat/py311.py` (27 lines)
**Purpose**: py311 module
**Key functions**: shuti

### `.venv_temp/lib/python3.14/site-packages/setuptools/compat/py312.py` (13 lines)
**Purpose**: py312 module

### `.venv_temp/lib/python3.14/site-packages/setuptools/compat/py39.py` (9 lines)
**Purpose**: py39 module

### `.venv_temp/lib/python3.14/site-packages/setuptools/config/__init__.py` (43 lines)
**Purpose**: For backward compatibility, expose main functions from
``setuptools.config.setup

### `.venv_temp/lib/python3.14/site-packages/setuptools/config/_apply_pyprojecttoml.py` (534 lines)
**Purpose**: Translation layer between pyproject config and setuptools distribution and
metad
**Classes**: _MissingDynamic
**Key functions**: apply

### `.venv_temp/lib/python3.14/site-packages/setuptools/config/_validate_pyproject/__init__.py` (34 lines)
**Purpose**: __init__ module
**Key functions**: valid

### `.venv_temp/lib/python3.14/site-packages/setuptools/config/_validate_pyproject/error_reporting.py` (338 lines)
**Purpose**: error_reporting module
**Classes**: ValidationError, _ErrorFormatting, _SummaryWriter
**Key functions**: detai

### `.venv_temp/lib/python3.14/site-packages/setuptools/config/_validate_pyproject/extra_validations.py` (151 lines)
**Purpose**: The purpose of this module is implement PEP 621 validations that are
difficult t
**Classes**: RedefiningStaticFieldAsDynamic, IncludedDependencyGroupMustExist, ImportNameCollision, ImportNameMissing
**Key functions**: valid

### `.venv_temp/lib/python3.14/site-packages/setuptools/config/_validate_pyproject/fastjsonschema_exceptions.py` (51 lines)
**Purpose**: fastjsonschema_exceptions module
**Classes**: JsonSchemaException, JsonSchemaValueException, JsonSchemaDefinitionException

### `.venv_temp/lib/python3.14/site-packages/setuptools/config/_validate_pyproject/formats.py` (464 lines)
**Purpose**: The functions in this module are used to validate schemas with the
`format JSON 
**Classes**: _TroveClassifier
**Key functions**: pep44

### `.venv_temp/lib/python3.14/site-packages/setuptools/config/expand.py` (452 lines)
**Purpose**: Utility functions to expand configuration directives or special values
(such glo
**Classes**: StaticModule, EnsurePackagesDiscovered, LazyMappingProxy
**Key functions**: glob_

### `.venv_temp/lib/python3.14/site-packages/setuptools/config/pyprojecttoml.py` (477 lines)
**Purpose**: Load setuptools configuration from ``pyproject.toml`` files.

**PRIVATE MODULE**
**Classes**: _ConfigExpander, _EnsurePackagesDiscovered, _ExperimentalConfiguration, _ToolsTypoInMetadata
**Key functions**: load_

### `.venv_temp/lib/python3.14/site-packages/setuptools/config/setupcfg.py` (782 lines)
**Purpose**: Load setuptools configuration from ``setup.cfg`` files.

**API will be made priv
**Classes**: ConfigHandler, ConfigMetadataHandler, ConfigOptionsHandler, _AmbiguousMarker, _DeprecatedConfig
**Key functions**: read_

### `.venv_temp/lib/python3.14/site-packages/setuptools/depends.py` (185 lines)
**Purpose**: depends module
**Classes**: Require
**Key functions**: maybe

### `.venv_temp/lib/python3.14/site-packages/setuptools/discovery.py` (614 lines)
**Purpose**: Automatic discovery of Python modules and packages (for inclusion in the
distrib
**Classes**: _Filter, _Finder, PackageFinder, PEP420PackageFinder, ModuleFinder, FlatLayoutPackageFinder, FlatLayoutModuleFinder, ConfigDiscovery
**Key functions**: remov

### `.venv_temp/lib/python3.14/site-packages/setuptools/dist.py` (1124 lines)
**Purpose**: dist module
**Classes**: Distribution, DistDeprecationWarning
**Key functions**: check

### `.venv_temp/lib/python3.14/site-packages/setuptools/errors.py` (67 lines)
**Purpose**: setuptools.errors

Provides exceptions used by setuptools modules.
**Classes**: InvalidConfigError, RemovedConfigError, RemovedCommandError, PackageDiscoveryError

### `.venv_temp/lib/python3.14/site-packages/setuptools/extension.py` (179 lines)
**Purpose**: extension module
**Classes**: Extension, Library

### `.venv_temp/lib/python3.14/site-packages/setuptools/glob.py` (185 lines)
**Purpose**: Filename globbing utility. Mostly a copy of `glob` from Python 3.5.

Changes inc
**Key functions**: glob,

### `.venv_temp/lib/python3.14/site-packages/setuptools/installer.py` (155 lines)
**Purpose**: installer module
**Classes**: _DeprecatedInstaller
**Key functions**: fetch

### `.venv_temp/lib/python3.14/site-packages/setuptools/launch.py` (36 lines)
**Purpose**: Launch the Python script on the command line after
setuptools is bootstrapped vi
**Key functions**: run

### `.venv_temp/lib/python3.14/site-packages/setuptools/logging.py` (40 lines)
**Purpose**: logging module
**Key functions**: confi

### `.venv_temp/lib/python3.14/site-packages/setuptools/modified.py` (18 lines)
**Purpose**: modified module

### `.venv_temp/lib/python3.14/site-packages/setuptools/monkey.py` (126 lines)
**Purpose**: Monkey patching of distutils.
**Key functions**: get_u

### `.venv_temp/lib/python3.14/site-packages/setuptools/msvc.py` (1557 lines)
**Purpose**: Environment info about Microsoft Compilers.

>>> getfixture('windows_only')
>>> 
**Classes**: PlatformInfo, RegistryInfo, SystemInfo, _EnvironmentDict, EnvironmentInfo, winreg

### `.venv_temp/lib/python3.14/site-packages/setuptools/namespaces.py` (101 lines)
**Purpose**: namespaces module
**Classes**: Installer, DevelopInstaller

### `.venv_temp/lib/python3.14/site-packages/setuptools/tests/__init__.py` (13 lines)
**Purpose**: __init__ module

### `.venv_temp/lib/python3.14/site-packages/setuptools/tests/compat/__init__.py` (0 lines)
**Purpose**: __init__ module

### `.venv_temp/lib/python3.14/site-packages/setuptools/tests/compat/py39.py` (3 lines)
**Purpose**: py39 module

### `.venv_temp/lib/python3.14/site-packages/setuptools/tests/config/__init__.py` (0 lines)
**Purpose**: __init__ module

### `.venv_temp/lib/python3.14/site-packages/setuptools/tests/config/downloads/__init__.py` (59 lines)
**Purpose**: __init__ module
**Key functions**: outpu

### `.venv_temp/lib/python3.14/site-packages/setuptools/tests/config/downloads/preload.py` (18 lines)
**Purpose**: This file can be used to preload files needed for testing.

For example you can 

### `.venv_temp/lib/python3.14/site-packages/setuptools/tests/config/test_apply_pyprojecttoml.py` (794 lines)
**Purpose**: Make sure that applying the configuration from pyproject.toml is equivalent to
a
**Classes**: TestLicenseFiles, TestPyModules, TestExtModules, TestDeprecatedFields, TestPresetField, TestMeta, TestInteropCommandLineParsing, TestStaticConfig
**Key functions**: maked

### `.venv_temp/lib/python3.14/site-packages/setuptools/tests/config/test_expand.py` (247 lines)
**Purpose**: test_expand module
**Classes**: TestReadAttr
**Key functions**: write

### `.venv_temp/lib/python3.14/site-packages/setuptools/tests/config/test_pyprojecttoml.py` (421 lines)
**Purpose**: test_pyprojecttoml module
**Classes**: TestEntryPoints, TestClassifiers, TestImportNames
**Key functions**: creat

### `.venv_temp/lib/python3.14/site-packages/setuptools/tests/config/test_pyprojecttoml_dynamic_deps.py` (111 lines)
**Purpose**: test_pyprojecttoml_dynamic_deps module
**Key functions**: test_

### `.venv_temp/lib/python3.14/site-packages/setuptools/tests/config/test_setupcfg.py` (987 lines)
**Purpose**: test_setupcfg module
**Classes**: ErrConfigHandler, TestConfigurationReader, TestMetadata, TestOptions, TestExternalSetters
**Key functions**: make_

### `.venv_temp/lib/python3.14/site-packages/setuptools/tests/contexts.py` (131 lines)
**Purpose**: contexts module
**Key functions**: tempd

### `.venv_temp/lib/python3.14/site-packages/setuptools/tests/environment.py` (95 lines)
**Purpose**: environment module
**Classes**: VirtualEnv
**Key functions**: run_s

### `.venv_temp/lib/python3.14/site-packages/setuptools/tests/fixtures.py` (406 lines)
**Purpose**: fixtures module
**Key functions**: user_

### `.venv_temp/lib/python3.14/site-packages/setuptools/tests/integration/__init__.py` (0 lines)
**Purpose**: __init__ module

### `.venv_temp/lib/python3.14/site-packages/setuptools/tests/integration/helpers.py` (80 lines)
**Purpose**: Reusable functions and classes for different types of integration tests.

For ex
**Classes**: Archive
**Key functions**: run, 

### `.venv_temp/lib/python3.14/site-packages/setuptools/tests/integration/test_pbr.py` (20 lines)
**Purpose**: test_pbr module
**Key functions**: test_

### `.venv_temp/lib/python3.14/site-packages/setuptools/tests/integration/test_pip_install_sdist.py` (223 lines)
**Purpose**: Integration tests for setuptools that focus on building packages via pip.

The i
**Key functions**: venv_

### `.venv_temp/lib/python3.14/site-packages/setuptools/tests/mod_with_constant.py` (1 lines)
**Purpose**: mod_with_constant module

### `.venv_temp/lib/python3.14/site-packages/setuptools/tests/namespaces.py` (90 lines)
**Purpose**: namespaces module
**Key functions**: iter_

### `.venv_temp/lib/python3.14/site-packages/setuptools/tests/script-with-bom.py` (1 lines)
**Purpose**: script-with-bom module

### `.venv_temp/lib/python3.14/site-packages/setuptools/tests/test_archive_util.py` (36 lines)
**Purpose**: test_archive_util module
**Key functions**: tarfi

### `.venv_temp/lib/python3.14/site-packages/setuptools/tests/test_bdist_deprecations.py` (28 lines)
**Purpose**: develop tests
**Key functions**: test_

### `.venv_temp/lib/python3.14/site-packages/setuptools/tests/test_bdist_egg.py` (73 lines)
**Purpose**: develop tests
**Classes**: Test
**Key functions**: setup

### `.venv_temp/lib/python3.14/site-packages/setuptools/tests/test_bdist_wheel.py` (708 lines)
**Purpose**: test_bdist_wheel module
**Classes**: simpler_bdist_wheel
**Key functions**: bdist

### `.venv_temp/lib/python3.14/site-packages/setuptools/tests/test_build.py` (33 lines)
**Purpose**: test_build module
**Classes**: Subcommand
**Key functions**: test_

### `.venv_temp/lib/python3.14/site-packages/setuptools/tests/test_build_clib.py` (84 lines)
**Purpose**: test_build_clib module
**Classes**: TestBuildCLib

### `.venv_temp/lib/python3.14/site-packages/setuptools/tests/test_build_ext.py` (293 lines)
**Purpose**: test_build_ext module
**Classes**: TestBuildExt, TestBuildExtInplace
**Key functions**: test_

### `.venv_temp/lib/python3.14/site-packages/setuptools/tests/test_build_meta.py` (959 lines)
**Purpose**: test_build_meta module
**Classes**: BuildBackendBase, BuildBackend, BuildBackendCaller, TestBuildMetaBackend, TestBuildMetaLegacyBackend
**Key functions**: test_

### `.venv_temp/lib/python3.14/site-packages/setuptools/tests/test_build_py.py` (480 lines)
**Purpose**: test_build_py module
**Classes**: TestTypeInfoFiles
**Key functions**: test_

### `.venv_temp/lib/python3.14/site-packages/setuptools/tests/test_config_discovery.py` (647 lines)
**Purpose**: test_config_discovery module
**Classes**: TestFindParentPackage, TestDiscoverPackagesAndPyModules, TestNoConfig, TestWithAttrDirective, TestWithCExtension, TestWithPackageData
**Key functions**: test_

### `.venv_temp/lib/python3.14/site-packages/setuptools/tests/test_core_metadata.py` (550 lines)
**Purpose**: test_core_metadata module
**Classes**: TestParityWithMetadataFromPyPaWheel, TestPEP643
**Key functions**: test_

### `.venv_temp/lib/python3.14/site-packages/setuptools/tests/test_depends.py` (15 lines)
**Purpose**: test_depends module
**Classes**: TestGetModuleConstant

### `.venv_temp/lib/python3.14/site-packages/setuptools/tests/test_develop.py` (113 lines)
**Purpose**: develop tests
**Classes**: TestNamespaces
**Key functions**: temp_

### `.venv_temp/lib/python3.14/site-packages/setuptools/tests/test_dist.py` (280 lines)
**Purpose**: test_dist module
**Key functions**: test_

### `.venv_temp/lib/python3.14/site-packages/setuptools/tests/test_dist_info.py` (147 lines)
**Purpose**: Test .dist-info style distributions.
**Classes**: TestDistInfo, TestWheelCompatibility
**Key functions**: run_c

### `.venv_temp/lib/python3.14/site-packages/setuptools/tests/test_distutils_adoption.py` (198 lines)
**Purpose**: test_distutils_adoption module
**Key functions**: win_s

### `.venv_temp/lib/python3.14/site-packages/setuptools/tests/test_editable_install.py` (1261 lines)
**Purpose**: test_editable_install module
**Classes**: TestLegacyNamespaces, TestPep420Namespaces, TestFinderTemplate, TestOverallBehaviour, TestLinkTree, TestCustomBuildPy, TestCustomBuildWheel, TestCustomBuildExt, MyBdistWheel, MyBuildExt
**Key functions**: edita

### `.venv_temp/lib/python3.14/site-packages/setuptools/tests/test_egg_info.py` (1306 lines)
**Purpose**: test_egg_info module
**Classes**: Environment, TestEggInfo, TestWriteEntries, RequiresTestHelper
**Key functions**: env

### `.venv_temp/lib/python3.14/site-packages/setuptools/tests/test_extern.py` (15 lines)
**Purpose**: test_extern module
**Key functions**: test_

### `.venv_temp/lib/python3.14/site-packages/setuptools/tests/test_find_packages.py` (218 lines)
**Purpose**: Tests for automatic package discovery
**Classes**: TestFindPackages, TestFlatLayoutPackageFinder
**Key functions**: ensur

### `.venv_temp/lib/python3.14/site-packages/setuptools/tests/test_find_py_modules.py` (73 lines)
**Purpose**: Tests for automatic discovery of modules
**Classes**: TestModuleFinder, TestFlatLayoutModuleFinder

### `.venv_temp/lib/python3.14/site-packages/setuptools/tests/test_glob.py` (45 lines)
**Purpose**: test_glob module
**Key functions**: test_

### `.venv_temp/lib/python3.14/site-packages/setuptools/tests/test_install_scripts.py` (89 lines)
**Purpose**: install_scripts tests
**Classes**: TestInstallScripts

### `.venv_temp/lib/python3.14/site-packages/setuptools/tests/test_logging.py` (76 lines)
**Purpose**: test_logging module
**Key functions**: test_

### `.venv_temp/lib/python3.14/site-packages/setuptools/tests/test_manifest.py` (622 lines)
**Purpose**: sdist tests
**Classes**: TempDirTestCase, TestManifestTest, TestFileListTest
**Key functions**: make_

### `.venv_temp/lib/python3.14/site-packages/setuptools/tests/test_namespaces.py` (79 lines)
**Purpose**: test_namespaces module
**Classes**: TestNamespaces

### `.venv_temp/lib/python3.14/site-packages/setuptools/tests/test_scripts.py` (12 lines)
**Purpose**: test_scripts module
**Classes**: TestWindowsScriptWriter

### `.venv_temp/lib/python3.14/site-packages/setuptools/tests/test_sdist.py` (980 lines)
**Purpose**: sdist tests
**Classes**: TestSdistTest, TestRegressions, CustomBuildPy, build_custom
**Key functions**: quiet

### `.venv_temp/lib/python3.14/site-packages/setuptools/tests/test_setopt.py` (40 lines)
**Purpose**: test_setopt module
**Classes**: TestEdit

### `.venv_temp/lib/python3.14/site-packages/setuptools/tests/test_setuptools.py` (294 lines)
**Purpose**: Tests for the 'setuptools' package
**Classes**: TestDepends, TestDistro
**Key functions**: isola

### `.venv_temp/lib/python3.14/site-packages/setuptools/tests/test_shutil_wrapper.py` (23 lines)
**Purpose**: test_shutil_wrapper module
**Key functions**: test_

### `.venv_temp/lib/python3.14/site-packages/setuptools/tests/test_unicode_utils.py` (10 lines)
**Purpose**: test_unicode_utils module
**Key functions**: test_

### `.venv_temp/lib/python3.14/site-packages/setuptools/tests/test_virtualenv.py` (113 lines)
**Purpose**: test_virtualenv module
**Key functions**: pytes

### `.venv_temp/lib/python3.14/site-packages/setuptools/tests/test_warnings.py` (106 lines)
**Purpose**: test_warnings module
**Classes**: _MyDeprecation
**Key functions**: test_

### `.venv_temp/lib/python3.14/site-packages/setuptools/tests/test_wheel.py` (690 lines)
**Purpose**: wheel tests
**Classes**: Record
**Key functions**: test_

### `.venv_temp/lib/python3.14/site-packages/setuptools/tests/test_windows_wrappers.py` (258 lines)
**Purpose**: Python Script Wrapper for Windows
=================================

setuptools 
**Classes**: WrapperTester, TestCLI, TestGUI
**Key functions**: win_l

### `.venv_temp/lib/python3.14/site-packages/setuptools/tests/text.py` (4 lines)
**Purpose**: text module
**Classes**: Filenames

### `.venv_temp/lib/python3.14/site-packages/setuptools/tests/textwrap.py` (6 lines)
**Purpose**: textwrap module
**Key functions**: DALS

### `.venv_temp/lib/python3.14/site-packages/setuptools/unicode_utils.py` (102 lines)
**Purpose**: unicode_utils module
**Classes**: _Utf8EncodingNeeded
**Key functions**: decom

### `.venv_temp/lib/python3.14/site-packages/setuptools/version.py` (6 lines)
**Purpose**: version module

### `.venv_temp/lib/python3.14/site-packages/setuptools/warnings.py` (110 lines)
**Purpose**: Provide basic warnings used by setuptools modules.

Using custom classes (other 
**Classes**: SetuptoolsWarning, InformationOnly, SetuptoolsDeprecationWarning

### `.venv_temp/lib/python3.14/site-packages/setuptools/wheel.py` (262 lines)
**Purpose**: Wheels support.
**Classes**: Wheel
**Key functions**: unpac

### `.venv_temp/lib/python3.14/site-packages/setuptools/windows_support.py` (30 lines)
**Purpose**: windows_support module
**Key functions**: windo

### `.venv_temp/lib/python3.14/site-packages/wheel/__init__.py` (3 lines)
**Purpose**: __init__ module

### `.venv_temp/lib/python3.14/site-packages/wheel/__main__.py` (25 lines)
**Purpose**: Wheel command line tool (enables the ``python -m wheel`` syntax)
**Key functions**: main

### `.venv_temp/lib/python3.14/site-packages/wheel/_bdist_wheel.py` (616 lines)
**Purpose**: Create a wheel (.whl) distribution.

A wheel is a built archive format.
**Classes**: bdist_wheel
**Key functions**: safe_

### `.venv_temp/lib/python3.14/site-packages/wheel/_commands/__init__.py` (169 lines)
**Purpose**: Wheel command-line utility.
**Key functions**: unpac

### `.venv_temp/lib/python3.14/site-packages/wheel/_commands/convert.py` (337 lines)
**Purpose**: convert module
**Classes**: ConvertSource, EggFileSource, EggDirectorySource, WininstFileSource
**Key functions**: conve

### `.venv_temp/lib/python3.14/site-packages/wheel/_commands/info.py` (124 lines)
**Purpose**: Display information about wheel files.
**Key functions**: info

### `.venv_temp/lib/python3.14/site-packages/wheel/_commands/pack.py` (84 lines)
**Purpose**: pack module
**Key functions**: pack,

### `.venv_temp/lib/python3.14/site-packages/wheel/_commands/tags.py` (140 lines)
**Purpose**: tags module
**Key functions**: tags

### `.venv_temp/lib/python3.14/site-packages/wheel/_commands/unpack.py` (30 lines)
**Purpose**: unpack module
**Key functions**: unpac

### `.venv_temp/lib/python3.14/site-packages/wheel/_metadata.py` (184 lines)
**Purpose**: Tools for converting old- to new-style metadata.
**Key functions**: yield

### `.venv_temp/lib/python3.14/site-packages/wheel/_setuptools_logging.py` (26 lines)
**Purpose**: _setuptools_logging module
**Key functions**: confi

### `.venv_temp/lib/python3.14/site-packages/wheel/bdist_wheel.py` (26 lines)
**Purpose**: bdist_wheel module

### `.venv_temp/lib/python3.14/site-packages/wheel/macosx_libfile.py` (486 lines)
**Purpose**: IMPORTANT: DO NOT IMPORT THIS MODULE DIRECTLY.
THIS IS ONLY KEPT IN PLACE FOR BA
**Classes**: SegmentBase, MachHeader, MachHeader, FatHeader, VersionMinCommand, FatArch, FatArch, VersionBuild
**Key functions**: swap3

### `.venv_temp/lib/python3.14/site-packages/wheel/metadata.py` (17 lines)
**Purpose**: metadata module

### `.venv_temp/lib/python3.14/site-packages/wheel/wheelfile.py` (252 lines)
**Purpose**: wheelfile module
**Classes**: WheelError, WheelFile
**Key functions**: urlsa

### `scripts/claude_debug_analysis.py` (118 lines)
**Purpose**: scripts/claude_debug_analysis.py — Called by auto-debug.yml GitHub Action.

Read
**Key functions**: main

### `scripts/export_diagnostics.py` (154 lines)
**Purpose**: Run ruff + pyright (+ eslint if frontend/ exists) and write DIAGNOSTICS.md
at pr
**Key functions**: main

### `scripts/extract_tagpack_seeds.py` (146 lines)
**Purpose**: GAP-015 on-chain pipeline, phase 1: extract exchange/miner seed addresses.

Sour
**Key functions**: extra

### `src/__init__.py` (1 lines)
**Purpose**: __init__ module

### `src/api/__init__.py` (1 lines)
**Purpose**: __init__ module

### `src/api/auth.py` (98 lines)
**Purpose**: API authentication — API key validation for REST and WebSocket.

All routes and 
**Key functions**: verif

### `src/api/main.py` (992 lines)
**Purpose**: FastAPI dashboard API.

Security: ALL endpoints require X-API-Key header matchin
**Classes**: AppState, ResolveApprovalRequest, SetExecutionModeRequest, SetRiskControlsRequest
**Key functions**: api_k

### `src/api/middleware.py` (52 lines)
**Purpose**: CORS validation middleware — prevents wildcard + credentials misconfiguration
an
**Key functions**: valid

### `src/config.py` (689 lines)
**Purpose**: Production configuration for the algorithmic trading bot.

Authority sources:
  
**Classes**: TradingMode, ExecutionMode, Timeframe, BinanceSettings, OKXSettings, RiskSettings, HMMSettings, XGBoostSettings, FeatureSettings, StorageSettings, APISettings, IntelligenceSettings, Settings, RuntimeConfig
**Key functions**: get_s

### `src/data/__init__.py` (1 lines)
**Purpose**: __init__ module

### `src/data/fetcher.py` (761 lines)
**Purpose**: Async market data fetcher — Binance (primary) + OKX (secondary).

Responsibiliti
**Classes**: OrderBookSnapshot, MarketDataFetcher, _FetcherContextManager
**Key functions**: open_

### `src/data/storage.py` (1324 lines)
**Purpose**: Async SQLite storage layer — aiosqlite, WAL mode, typed queries.

Schema owns fi
**Classes**: BarRecord, TradeRecord, RegimeSnapshotRecord, ModelMetricsRecord, EquityRecord, StorageBackend

### `src/diagnostics/__init__.py` (0 lines)
**Purpose**: __init__ module

### `src/diagnostics/runtime_monitor.py` (308 lines)
**Purpose**: Runtime Monitor — continuous async health diagnostics with auto-healing.

Respon
**Classes**: ProbeResult, HealthSnapshot, RuntimeMonitor
**Key functions**: get_m

### `src/diagnostics/signal_debugger.py` (424 lines)
**Purpose**: Signal Debugger — feature drift detection, model degradation scanner,
          
**Classes**: FeatureDriftRecord, FeatureDriftMonitor, PredictionRecord, ModelDegradationTracker, LabelShiftRecord, LabelShiftDetector
**Key functions**: run_p

### `src/diagnostics/trade_auditor.py` (270 lines)
**Purpose**: Trade Auditor — captures every signal decision with full diagnostic context.

Ev
**Classes**: AuditRecord, TradeAuditor
**Key functions**: get_a

### `src/engine/__init__.py` (1 lines)
**Purpose**: __init__ module

### `src/engine/orchestrator.py` (807 lines)
**Purpose**: Orchestrator — top-level async event loop coordinating all subsystems.

Responsi
**Classes**: Orchestrator

### `src/engine/signal_engine.py` (692 lines)
**Purpose**: Signal engine — per-timeframe signal computation pipeline.

On every tick for a 
**Classes**: SignalResult, SignalEngine

### `src/execution/__init__.py` (1 lines)
**Purpose**: __init__ module

### `src/execution/base.py` (118 lines)
**Purpose**: Abstract base class for trade executors (VUL-038).

Both LiveExecutor and PaperE
**Classes**: AbstractExecutor

### `src/execution/live.py` (985 lines)
**Purpose**: Live trading executor — real money order placement via ccxt.

Mirrors PaperExecu
**Classes**: LivePosition, ApprovalRequest, LiveExecutor

### `src/execution/live_fsm_integration.py` (105 lines)
**Purpose**: Live Executor FSM Integration — refactored order placement with OrderFSM.

Repla
**Classes**: LiveExecutorOrderFSM

### `src/execution/order_fsm.py` (300 lines)
**Purpose**: Order Finite State Machine — formalized order lifecycle with state transitions.

**Classes**: OrderStatus, OrderFSMError, OrderFSMState, OrderFSM

### `src/execution/order_manager.py` (256 lines)
**Purpose**: Order Manager — FSM-based order lifecycle management for live executor.

Wraps c
**Classes**: OrderManager

### `src/execution/paper.py` (912 lines)
**Purpose**: Paper trading executor.

Simulates trade execution against live market prices wi
**Classes**: PaperPosition, ApprovalRequest, PaperExecutor

### `src/features/__init__.py` (1 lines)
**Purpose**: __init__ module

### `src/features/intelligence_features.py` (153 lines)
**Purpose**: Intelligence-augmented features.

Extends core feature pipeline (9 features) wit
**Classes**: IntelligenceFeatureMatrix
**Key functions**: add_i

### `src/features/pipeline.py` (832 lines)
**Purpose**: Feature engineering pipeline.

Implements every feature from the signal architec
**Classes**: TripleBarrierResult, FeatureMatrix
**Key functions**: fract

### `src/intelligence/__init__.py` (18 lines)
**Purpose**: Crypto intelligence layer — on-chain metrics, exchange flows, whale tracking.

P

### `src/intelligence/causal_inference.py` (469 lines)
**Purpose**: Causal inference framework.

Answer causal questions, not just correlations:
- D
**Classes**: CausalEffect, CausalDAG, CausalInferenceEngine

### `src/intelligence/client.py` (670 lines)
**Purpose**: Multi-provider intelligence client aggregator.

Responsibilities:
  - Manage cre
**Classes**: CacheEntry, IntelligenceAggregator
**Key functions**: get_i

### `src/intelligence/ensemble_predictor.py` (654 lines)
**Purpose**: Ensemble prediction framework.

Reduce model risk by combining diverse predictio
**Classes**: EnsemblePrediction, PredictionModel, ARIMAPredictor, XGBoostPredictor, LSTMPredictor, GaussianProcessPredictor, TreeEnsemblePredictor, EnsemblePredictor, _LSTMNet

### `src/intelligence/metrics.py` (284 lines)
**Purpose**: Intelligence metrics computation layer.

Transforms raw provider data into tradi
**Classes**: IntelligenceMetrics, IntelligenceAnalyzer

### `src/intelligence/probabilistic.py` (444 lines)
**Purpose**: Probabilistic inference engine for crypto intelligence.

Replaces deterministic 
**Classes**: ProbabilisticPrediction, RiskAssessment, BayesianExchangeStressModel, BayesianWhaleActivityModel, BayesianRegimeDetection

### `src/intelligence/providers/__init__.py` (10 lines)
**Purpose**: Crypto intelligence providers.

Each provider wraps a specific API:
  - glassnod

### `src/intelligence/providers/binance_provider.py` (482 lines)
**Purpose**: Binance public REST provider for intelligence metrics.

All endpoints used here 
**Classes**: BinanceIntelligenceProvider
**Key functions**: get_b

### `src/intelligence/risk_quantification.py` (342 lines)
**Purpose**: Risk quantification and uncertainty analysis.

Measures: VaR, CVaR, stress testi
**Classes**: RiskMetrics, RiskQuantifier

### `src/regime/__init__.py` (1 lines)
**Purpose**: __init__ module

### `src/regime/detector.py` (709 lines)
**Purpose**: GaussianHMM regime detector — Hamilton (1989) 3-state switching model.

States:

**Classes**: RegimePrediction, RegimeDetector

### `src/risk/__init__.py` (1 lines)
**Purpose**: __init__ module

### `src/risk/cognitive_engine.py` (748 lines)
**Purpose**: Cognitive Engine — Mandatory Runtime Decision Layer
============================
**Classes**: ValidatorStatus, ValidatorResult, CognitiveDecision, SignalContext, QuantValidator, ProbabilityValidator, RiskValidator, BlockchainValidator, RegimeValidator, CognitiveEngine
**Key functions**: get_c

### `src/risk/drift_integration.py` (120 lines)
**Purpose**: Drift Detector Integration — hooks for orchestrator to record trade outcomes.

A
**Classes**: DriftIntegrationAdapter

### `src/risk/gates.py` (937 lines)
**Purpose**: Risk gate engine — hard limits that block new positions.

Gates (all must pass f
**Classes**: GateStatus, GateResult, RiskGateContext, DrawdownTracker
**Key functions**: check

### `src/risk/kelly.py` (604 lines)
**Purpose**: Kelly position sizing — half-Kelly with hard ceiling.

Kelly (1956) "A New Inter
**Classes**: KellyResult
**Key functions**: kelly

### `src/risk/performance_drift.py` (343 lines)
**Purpose**: Performance Drift Trigger — detects model decay in live trading.

Monitors:
  1.
**Classes**: PerformanceBaseline, DriftDetected, PerformanceDriftDetector

### `src/risk/portfolio_correlation.py` (313 lines)
**Purpose**: Portfolio Correlation Layer — Gap-005.

Tracks rolling pairwise return correlati
**Classes**: _EWMSeries, _EWMCov, PortfolioCorrelationTracker
**Key functions**: get_p

### `src/risk/slippage.py` (228 lines)
**Purpose**: Slippage and market-impact model — Almgren-Chriss square-root impact.

GAP-001: 
**Classes**: SlippageEstimate, SlippageModel

### `src/strategies/__init__.py` (0 lines)
**Purpose**: __init__ module

### `src/strategies/filters.py` (468 lines)
**Purpose**: Professional strategy filters and signal enrichment.

Implements research-backed
**Key functions**: ewm_t

### `src/strategies/position_sizing.py` (288 lines)
**Purpose**: Advanced position sizing — Carver (2019) and López de Prado (2018).

Implements 
**Key functions**: carve

### `tests/test_api_and_fsm_coverage.py` (275 lines)
**Purpose**: Coverage for small zero-coverage modules — Debt-005.

Covers:
  - src/api/middle
**Classes**: TestValidateCorsConfig, TestGetConfiguredKey, TestVerifyApiKey, TestVerifyWsKey, TestLiveExecutorOrderFSMInit, TestLiveExecutorOrderFSMPlaceOrder

### `tests/test_cognitive_engine.py` (431 lines)
**Purpose**: Tests for src/risk/cognitive_engine.py — mandatory five-validator decision
layer
**Classes**: TestQuantValidator, TestProbabilityValidator, TestRiskValidator, TestBlockchainValidator, TestRegimeValidator, TestCognitiveEngineAggregation
**Key functions**: reset

### `tests/test_detector.py` (334 lines)
**Purpose**: Tests for src/regime/detector.py — GaussianHMM 3-state regime detector
(Hamilton
**Classes**: TestRegimePredictionEntropyMath, TestPositionScalar, TestRegimeDetectorFit, TestRegimeDetectorPredict, TestRegimeDetectorPersistence, TestRegimeStatistics
**Key functions**: reset

### `tests/test_drift_integration_coverage.py` (102 lines)
**Purpose**: Coverage for src/risk/drift_integration.py — Debt-005.
**Classes**: TestDriftIntegrationAdapterInit, TestRecordClosedTrade, TestCheckDrift

### `tests/test_features.py` (419 lines)
**Purpose**: Tests for src/features/pipeline.py — all feature functions and the full pipeline
**Classes**: TestFracDiffWeights, TestFractionalDifferentiation, TestVWAPDevZscore, TestOrderFlowImbalance, TestRealizedVolRatio, TestATRMomentum, TestRollingSharpe, TestVolumeZscore, TestComputeDailyVol, TestTripleBarrierLabels, TestMetaLabels, TestBuildFeatureMatrix, TestBuildInferenceFeatures
**Key functions**: reset

### `tests/test_integration_full_pipeline.py` (288 lines)
**Purpose**: End-to-end integration tests for full trading pipeline.

Tests:
  - Order placem
**Classes**: TestDriftGateIntegration, TestDriftIntegrationAdapter, TestOrderFSMInContext, TestOrderFSMStateSnapshot

### `tests/test_intelligence_metrics.py` (170 lines)
**Purpose**: Regression tests for two bugs found and fixed this session (GAP-015 follow-on):

**Classes**: TestWhaleTakerRatioFix, TestComputeMetricsConfidenceFix

### `tests/test_kelly.py` (515 lines)
**Purpose**: Tests for src/risk/kelly.py — Kelly formula, sizing, win/loss stats.
**Classes**: TestKellyFraction, TestHalfKellyFraction, TestKellyFromModelProbs, TestFloorToPrecision, TestSizePosition, TestComputePositionSize, TestComputeWinLossStats
**Key functions**: reset

### `tests/test_kelly_gaps.py` (288 lines)
**Purpose**: Targeted tests closing remaining coverage gaps in src/risk/kelly.py.

Companion 
**Classes**: TestKellyResultPositionSizePct, TestHalfKellyFractionBoundsValidation, TestKellyFromModelProbsInvalidDirection, TestKellyFromModelProbsNonFinitePLong, TestKellyFromModelProbsNonFiniteWinLossRatio, TestSizePositionMinAmountRejection, TestSizePositionMaxPositionPctValidation, TestComputePositionSizeDefaultCfg, TestComputeWinLossStatsAllWinsOrAllLosses
**Key functions**: cfg

### `tests/test_live_executor_fsm.py` (274 lines)
**Purpose**: Integration tests for LiveExecutor with OrderFSM.

Tests that _place_market_orde
**Classes**: TestOrderManagerMock, TestFSMStateTransitions, TestOrderReconciliation

### `tests/test_model_trainer_coverage.py` (263 lines)
**Purpose**: Coverage for src/models/trainer.py — Debt-005.

Targets predict_direction, predi
**Classes**: TestModelTrainerInit, TestPredictDirection, TestPredictMeta, TestComputeWinLossStats, TestTrainingResult

### `tests/test_orchestrator.py` (199 lines)
**Purpose**: Tests for src/engine/orchestrator.py

Focus: correlation scalar computation (GAP
**Classes**: TestPortfolioCorrelationTracker, TestOrchestratorCorrelationState, TestCorrelationScalarFailSafe

### `tests/test_order_fsm.py` (296 lines)
**Purpose**: Test suite for Order FSM state machine.
**Classes**: TestOrderFSMBasics, TestOrderFSMTransitions, TestPartialFills, TestRetryCounter

### `tests/test_order_fsm_registry.py` (191 lines)
**Purpose**: Tests for the order FSM registry follow-up to GAP-004, and the two
endpoints (GE
**Classes**: TestOrderFSMRegistry, TestOrderStatusEndpoint, TestPerformanceDriftEndpoint, _FakeExecutor, _FakeDriftAdapter, _FakeOrchestrator
**Key functions**: api_c

### `tests/test_paper_executor.py` (532 lines)
**Purpose**: Test coverage for src/execution/paper.py — paper trading executor.
**Classes**: TestPaperPosition, TestApprovalRequestToDict, TestLifecycle, TestSubmitSignalAutomatic, TestSubmitSignalRestricted, TestSubmitSignalManual, TestClosePosition, TestMarkToMarket, TestApprovalQueueManagement, TestApprovalTimeout, TestStateQueriesAndProperties
**Key functions**: make_

### `tests/test_performance_drift.py` (179 lines)
**Purpose**: Test suite for Performance Drift Detector.
**Classes**: TestPerformanceBaseline, TestDriftDetector, TestSharpeDrift, TestAccuracyDrift, TestLiveMetrics, TestModelDegradationTracker

### `tests/test_portfolio_correlation.py` (364 lines)
**Purpose**: Tests for src/risk/portfolio_correlation.py

Covers: _EWMSeries, _EWMCov, Portfo
**Classes**: TestEWMSeries, TestEWMCov, TestPushBarReturns, TestPushReturn, TestCorrelation, TestAvgCorrelation, TestCorrelationScalar, TestCorrelationMatrix, TestSingleton

### `tests/test_position_sizing.py` (421 lines)
**Purpose**: Test coverage for src/strategies/position_sizing.py — Carver/AFML/Thorp sizing.
**Classes**: TestCarverForecastPosition, TestVolTargetQuantity, TestEstimateDailyVol, TestCorrelationAdjustedNotional, TestAfmlBetSize, TestThorpKellyWithVariance, TestRecommendPositionNotional

### `tests/test_risk_controls_api.py` (380 lines)
**Purpose**: Tests for GAP-013: runtime-toggleable position-exit controls.

Covers:
  - check
**Classes**: TestCheckPositionExit, TestRuntimeConfigRiskControls, TestRiskControlsEndpoints, _FakeStorage
**Key functions**: api_c

### `tests/test_risk_gates.py` (331 lines)
**Purpose**: Tests for src/risk/gates.py — all risk gate functions and the full stack.
**Classes**: TestDailyDrawdown, TestConsecutiveLosses, TestRegimeGate, TestPositionSize, TestLiveGate, TestPaperMinimumDays, TestEvaluateAllGates, TestDrawdownTracker, TestSlippageVetoGate, TestEvaluateAllGatesSlippageWiring
**Key functions**: reset

### `tests/test_risk_gates_coverage.py` (246 lines)
**Purpose**: Additional coverage for src/risk/gates.py — Debt-005.

Tests individual gate fun
**Classes**: TestSlippageVeto, TestDailyDrawdown, TestConsecutiveLosses, TestRegimeGate, TestPositionSize, TestGateResult, TestEvaluateAllGates

### `tests/test_signal_engine.py` (480 lines)
**Purpose**: Test coverage for src/engine/signal_engine.py — Debt-005.

Strategy: mock all ex
**Classes**: TestSkipShape, TestSkipPaths, TestTradeablePath, TestModelSwap, TestTask010FundingRateWiring

### `tests/test_slippage.py` (165 lines)
**Purpose**: Tests for src/risk/slippage.py — Almgren-Chriss slippage/impact model.
**Classes**: TestEstimate, TestVetoIfNegativeEv
**Key functions**: reset

### `tests/test_storage.py` (655 lines)
**Purpose**: Test coverage for src/data/storage.py — async SQLite storage backend.
**Classes**: TestRecordConstructors, TestInitializeAndClose, TestOpenStorageContextManager, TestBars, TestTrades, TestRegimeSnapshots, TestModelMetrics, TestEquityCurve, TestValidateSymbol, TestAuditLog, TestHealthCheck, TestSchemaMigrations
**Key functions**: make_

### `tests/test_strategies_filters.py` (288 lines)
**Purpose**: Test coverage for src/strategies/filters.py — research-backed signal filters.
**Classes**: TestEwmTrendSignal, TestTrendFilterPasses, TestVolAdjustedMomentum, TestOvernightGapIsExcessive, TestRegimePositionScaler, TestHurstExponent, TestHurstFilterPasses, TestObvTrendConfirms, TestVolExplosionBlocks, TestMtfTrendAligned

### `tests/test_trade_auditor.py` (250 lines)
**Purpose**: Coverage for:
  - src/diagnostics/trade_auditor.py
  - src/risk/intelligence_gat
**Classes**: TestAuditRecord, TestTradeAuditor, TestDetectAnomalies

## Risk Architecture
Gates execute sequentially — first fail short-circuits remaining gates:
1. Daily drawdown halt: 2% of starting equity
2. Consecutive loss halt: 3 trades
3. Regime gate: block when HMM state = volatile
4. Max position size: 5% of capital
5. Paper minimum: 30 days required
6. Live gate: OOS Sharpe > 1.5, max DD < 15%, 500+ trades

## Execution Modes
- AUTOMATIC: fires within risk gates, no approval
- RESTRICTED: auto below notional limit, approval above, 30s timeout skip
- MANUAL: every trade queued for operator approval

## Timeframes
- 1m: scalping, paper only
- 15m: primary real-money intraday
- 4h: swing, paper only

## Key Design Decisions (ADR)
- ADR-001: Triple-barrier + CPCV chosen over simple train/test (eliminates lookahead + serial correlation)
- ADR-002: Meta-labeling separates direction from bet confidence
- ADR-003: Fractional diff d=0.4 balances stationarity and memory preservation
- ADR-004: Half-Kelly at 0.5× with 25% ceiling — Thorp conservative for single-strategy
- ADR-005: SQLite WAL for development; migration path to TimescaleDB for live scale
- ADR-006: Paper mode default — live requires explicit env var + gate pass

## Known Gaps (open architecture items)
- GAP-001: No slippage/market-impact model in live.py (Almgren-Chriss needed)
- GAP-002: HMM regime has no posterior entropy gate (confidence not quantified)
- GAP-003: KS-test drift detection misses label shift (performance-based trigger needed)
- GAP-004: No order state machine (PENDING→FILLED FSM) in live executor
- GAP-005: No portfolio correlation layer for multi-symbol operation
- GAP-006: SQLite write contention under high-frequency multi-timeframe load