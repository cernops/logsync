# Specific for RHEL/Alma 9 at CERN

Name:           logsync
Version:        2.0.0
Release:        1%{?dist}
Summary:        Drain redirected HTCondor UserLog files to their intended destination

License:        Apache-2.0
URL:            https://github.com/cernops/logsync
Source:         %{name}-%{version}.tgz

BuildArch:      noarch
BuildRequires:  python3.14
BuildRequires:  python3.14-pytest
BuildRequires:  systemd-rpm-macros
Requires:       python3.14
%{?systemd_requires}

%description
Logsync drains redirected HTCondor UserLog files while the schedd and shadows continue to append new records.
It durably copies complete records to their intended destinations, frees copied ranges with hole punching,
and collects fully drained source files when no writer has them open.
It can reuse the HTCondor-managed keyring credentials when accessing AFS destinations.

%prep
%autosetup -n %{name}-%{version}

%build

%install
install -d %{buildroot}%{_prefix}/lib/python3.14/site-packages
cp -a src/logsync %{buildroot}%{_prefix}/lib/python3.14/site-packages/

install -Dpm 0755 bin/logsync %{buildroot}%{_bindir}/logsync

%check
#PYTHONPATH=src %{_bindir}/python3.14 -m unittest discover -b -s tests/unittests -t .
#PYTHONPATH=src %{_bindir}/python3.14 -m pytest -q tests/pytests
PYTHONPATH=%{buildroot}%{_prefix}/lib/python3.14/site-packages %{buildroot}%{_bindir}/logsync --help >/dev/null

%postun
%systemd_postun_with_restart logsync.service

%files
%license LICENSE
%doc NOTICE README.md docs/README.md docs/METRICS.md
%{_bindir}/logsync
%{_prefix}/lib/python3.14/site-packages/logsync/

%changelog
* Fri Aug 21 2026 Panagiotis Gkonis <panagiotis.gkonis@cern.ch> - 2.0.0-1
- Release under license Apache 2.0
- Write as target user - GPT 5.6 Sol (medium)
- Switch to euid/egid instead of fsuid/fsgid
- Switch euid/egid at beginning of user loop
- Fix euid/egid switch ordering and add root tests
- Un-join user AFS token when done, just in case
- Exceptions when restoring effective identity should be fatal
- Supplementary group support
- Remove --test-access
- Use neutral keyring during global phases and exit upon fatal euid/egid switch failure
- Do not run root tests by doing eval on strings of python code
- CI: make non-koji rpm build manual
- Documentation updates

* Fri Aug 14 2026 Panagiotis Gkonis <panagiotis.gkonis@cern.ch> - 1.0.3-1
- Restart logsync.service upon package update (if exists)

* Fri Aug 14 2026 Panagiotis Gkonis <panagiotis.gkonis@cern.ch> - 1.0.2-1
- Metrics: make global phase sleep always present

* Fri Aug 14 2026 Panagiotis Gkonis <panagiotis.gkonis@cern.ch> - 1.0.1-1
- Shuffle user's logs on each pass to avoid starvation

* Tue Aug 11 2026 Panagiotis Gkonis <panagiotis.gkonis@cern.ch> - 1.0.0-1
- Initial release
