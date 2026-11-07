Name:           simple-cuts
Version:        %{?app_version}%{!?app_version:0.2.0}
Release:        1%{?dist}
Summary:        Simple video cutter for camera footage
License:        MIT
BuildArch:      noarch

# Fedora package names. ffmpeg is required by path so both RPM Fusion's ffmpeg and
# Fedora's ffmpeg-free satisfy it.
Requires:       python3 >= 3.9
Requires:       python3-gobject
Requires:       gtk4
Requires:       gstreamer1
Requires:       gstreamer1-plugins-base
Requires:       gstreamer1-plugins-good
Requires:       /usr/bin/ffmpeg
Requires:       /usr/bin/ffprobe
Recommends:     gstreamer1-plugin-libav
Recommends:     gstreamer1-plugins-bad-free

%description
Open camera clips, mark the parts to keep and export them (optionally without
audio) before importing into a video editor. Lossless stream-copy or
frame-accurate re-encoding (NVIDIA NVENC or CPU).

%prep
# Sources are staged by packaging/rpm/build-rpm.sh

%build

%install
%{_sourcedir}/packaging/stage.sh %{buildroot} %{_prefix}

%files
%license %{_datadir}/licenses/simple-cuts/LICENSE
%{_bindir}/simple-cuts
%{_prefix}/lib/simple-cuts/
%{_datadir}/applications/io.github.simplecuts.SimpleCuts.desktop
%{_datadir}/icons/hicolor/scalable/apps/io.github.simplecuts.SimpleCuts.svg
%{_datadir}/metainfo/io.github.simplecuts.SimpleCuts.metainfo.xml

%changelog
* Sat Nov 07 2026 Bitangsha Ray <plaintextlab@gmail.com> - 0.2.0-1
- GTK4 interface, all-in-one launcher, distro packages
