import yaml
import tqdm
import mne.io as loader

from pathlib import Path
from mne import pick_types
from .dataset import Dataset, RawTorchRecording, EpochTorchRecording, Thinker
from data.utils import make_epochs_from_raw


_SUPPORTED_EXTENSIONS = {
    '.edf': loader.read_raw_edf,
    # FIXME: need to handle part fif files
    '.fif': loader.read_raw_fif,
    # TODO: add much more support, at least all of MNE-python
}


class DN3ConfigException(BaseException):
    """
    Exception to be triggered when DN3-configuration parsing fails.
    """
    pass


class ExperimentConfig:
    """
    Parses DN3 configuration files. Checking the DN3 token for listed datasets.
    """
    def __init__(self, config_filename: str, adopt_auxiliaries=True):
        """
        Parses DN3 configuration files. Checking the DN3 token for listed datasets.
        Parameters
        ----------
        config_filename : str
                          String for path to yaml formatted configuration file
        adopt_auxiliaries : bool
                             For any additional tokens aside from DN3 and specified datasets, integrate them into this
                             object for later use. Defaults to True. This will propagate for the detected datasets.
        """
        with open(config_filename, 'r') as fio:
            self._original_config = yaml.load(fio)
        working_config = self._original_config.copy()

        if 'DN3' not in working_config.keys():
            raise DN3ConfigException("Toplevel `DN3` not found in: {}".format(config_filename))
        if 'datasets' not in working_config['DN3'].keys():
            raise DN3ConfigException("`datasets` not found in {}".format([k.lower() for k in
                                                                          working_config["DN3"].keys()]))
        if not isinstance(working_config['DN3']['datasets'], list):
            raise DN3ConfigException("`datasets` must be a list")
        self._dataset_names = working_config['DN3']['datasets']

        self.datasets = dict()
        for ds in self._dataset_names:
            if ds not in working_config.keys():
                raise DN3ConfigException("Dataset: {} not found in {}".format(
                    ds, [k for k in working_config.keys() if k != 'DN3']))
            self.datasets[ds] = DatasetConfig(ds, working_config.pop(ds))
        print("Found {} dataset(s).".format(len(self.datasets)))

        self.experiment = working_config.pop('DN3')
        if adopt_auxiliaries:
            self.__dict__.update(working_config)


class DatasetConfig:
    """
    Parses dataset entries in DN3 config
    """
    def __init__(self, name: str, config: dict, adopt_auxiliaries=True, ext_handlers=None):
        """
        Parses dataset entries in DN3 config
        Parameters
        ----------
        name : str
               The name of the dataset specified in the config. Will be replaced if the optional `name` field is present
               in the config.
        config : dict
                The configuration entry for the dataset
        ext_handlers : dict, optional
                       If specified, should be a dictionary that maps file extensions (with dot e.g. `.edf`) to a
                       callable that returns a `raw` instance given a string formatted path to a file.
        adopt_auxiliaries : bool
                            Adopt additional configuration entries as object variables.

        """
        self._original_config = dict(config).copy()

        # Optional args set, these help define which are required, so they come first
        def get_pop(key, default=None):
            config.setdefault(key, default)
            return config.pop(key)

        # Epoching relevant options
        # self.tlen = get_pop('tlen')
        self.tmin = get_pop('tmin')
        self._create_raw_recordings = self.tmin is None
        self.events = get_pop('events')
        if self.events is not None and not isinstance(self.events, list):
            raise DN3ConfigException("Specifying desired events must be done as a list. Not {}.".format(self.events))
        self.picks = get_pop('picks')
        if self.picks is not None and not isinstance(self.events, list):
            raise DN3ConfigException("Specifying picks must be done as a list. Not {}.".format(self.events))
        self.decimate = get_pop('decimate', 1)
        self.baseline = get_pop('baseline')
        self.bandpass = get_pop('bandpass')
        self.drop_bad = get_pop('drop_bad', False)

        # other options
        self.data_max = get_pop('max')
        self.data_min = get_pop('min')
        self.name = get_pop('name', name)
        self.stride = get_pop('stride', 1)
        self.extensions = get_pop('file_extensions', list(_SUPPORTED_EXTENSIONS.keys()))
        self.exclude_people = get_pop('exclude_people', list())
        self.exclude_sessions = get_pop('exclude_sessions', list())

        # Required args
        try:
            self.toplevel = Path(config.pop('toplevel'))
            self.tlen = config.pop('tlen')
        except KeyError as e:
            raise DN3ConfigException("Could not find required value: {}".format(e.args[0]))
        if not self.toplevel.exists():
            raise DN3ConfigException("The toplevel {} for dataset {} does not exists".format(self.toplevel, self.name))

        # The rest
        if adopt_auxiliaries:
            print("Adding additional configuration entries: {}".format(config.keys()))
            self.__dict__.update(config)

        self._extension_handlers = _SUPPORTED_EXTENSIONS.copy()
        if ext_handlers is not None:
            for ext in ext_handlers:
                self.add_extension_handler(ext, ext_handlers[ext])

    _PICK_TYPES = ['meg', 'eeg', 'stim', 'eog', 'ecg', 'emg', 'ref_meg', 'misc', 'resp', 'chpi', 'exci', 'ias', 'syst',
                   'seeg', 'dipole', 'gof', 'bio', 'ecog', 'fnirs', 'csd', ]

    def _picks_as_types(self):
        for pick in self.picks:
            if pick not in self._PICK_TYPES:
                return False
        return True

    def add_extension_handler(self, extension: str, handler):
        """
        Provide callable code to create a raw instance from sessions with certain file extensions. This is useful for
        handling of custom file formats, while preserving a consistent experiment framework.

        Parameters
        ----------
        extension : str
                   An extension that includes the '.', e.g. '.csv'
        handler : callable
                  Callback with signature f(path_to_file: str) -> mne.io.Raw

        Returns
        -------

        """
        assert callable(handler)
        self._extension_handlers[extension] = handler

    def scan_toplevel(self):
        files = list()
        pbar = tqdm.tqdm(self.extensions,
                         desc="Scanning {}. If there are a lot of files, this may take a while...".format(
                             self.toplevel))
        for extension in pbar:
            pbar.set_postfix(dict(extension=extension))
            files += self.toplevel.glob("**/*{}".format(extension))
        return files

    def auto_mapping(self, files=None):
        """
        Generates a mapping of sessions and people of the dataset, assuming files are stored in the structure:
         `toplevel`/(*optional - <version>)/<person-id>/<session-id>.{ext}
        Parameters
        -------
        files : list
                Optional list of files (convertible to `Path` objects, e.g. relative or absolute strings) to be used.
                If not provided, will use `scan_toplevel()`.
        Returns
        -------
        mapping : dict
                  The keys are of all the people in the dataset, and each value another similar mapping to that person's
                  sessions.
        """
        files = self.scan_toplevel() if files is None else files
        mapping = dict()
        for sess_file in files:
            sess_file = Path(sess_file)
            person = sess_file.parent.name
            if person in mapping:
                mapping[person].append(str(sess_file))
            else:
                mapping[person] = [str(sess_file)]
        return mapping

    def _load_raw(self, path: Path):
        if path.suffix in self._extension_handlers:
            return self._extension_handlers[path.suffix](str(path))
        print("Handler for file {} with extension {} not found.".format(str(path), path.suffix))
        for ext in path.suffixes:
            if ext in self._extension_handlers:
                print("Trying {} instead...".format(ext))
                return self._extension_handlers[ext]

        raise DN3ConfigException("No supported/provided loader found for {}".format(str(path)))

    def _construct_session_from_config(self, session):
        if not isinstance(session, Path):
            session = Path(session)

        raw = self._load_raw(session)
        if self._create_raw_recordings:
            return RawTorchRecording(raw, sample_len=int(self.tlen * raw.info['sfreq']), stride=self.stride)

        epochs = make_epochs_from_raw(raw, self.tmin, self.tlen, baseline=self.baseline, decim=self.decimate,
                                      filter_bp=self.bandpass, drop_bad=self.drop_bad)
        picks = pick_types(raw.info, **{t: t in self.picks for t in self._PICK_TYPES}) if self._picks_as_types() \
            else self.picks

        return EpochTorchRecording(epochs, picks=picks)

    def _construct_thinker_from_config(self, thinker: list):
        return Thinker({Path(sess).name: self._construct_session_from_config(sess) for sess in thinker})

    def auto_construct_dataset(self, mapping=None, **dsargs):
        """
        This creates a dataset using the config values. If tlen and tmin are specified in the config, creates epoched
        dataset, otherwise Raw.

        Parameters
        ----------
        mapping : dict, optional
                A dict specifying a list of sessions (as paths to files) for each person_id in the dataset. e.g.
                {
                  person_1: [sess_1.edf, ...],
                  person_2: [sess_1.edf],
                  ...
                }
                If not specified, will use `auto_mapping()` to generate.
        dsargs :
                Any additional arguments to feed for the creation of the dataset. i.e. keyword arguments to `Dataset`'s
                constructor (which id's to return).

        Returns
        -------
        dataset : :any:`Dataset`
                An instance of :any:`Dataset`, constructed according to mapping.
        """
        if mapping is None:
            return self.auto_construct_dataset(self.auto_mapping())

        print("Creating dataset of {} {} recordings from {} people.".format(sum(len(p) for p in mapping),
                                                                            "Raw" if self._create_raw_recordings else
                                                                            "Epoched", len(mapping)))
        description = "Loading {}".format(self.name)
        return Dataset({t: self._construct_thinker_from_config(mapping[t]) for t in tqdm.tqdm(mapping,
                                                                                              desc=description,
                                                                                              unit='person')},
                       **dsargs)
