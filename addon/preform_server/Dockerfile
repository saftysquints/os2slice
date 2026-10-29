# Formlabs' own PreForm Server for Linux (Docker Hub, formlabs/preform-server). Built on
# this machine from Formlabs' image; never push the result anywhere (the image bundles
# PreForm). Pinned: upgrade by changing the tag, the add-on version and the target's
# `version` together, after checking the release notes and the Form 4's firmware.
FROM formlabs/preform-server:release-3.64.1.664
ENV DISABLE_PREFORMSERVER_TELEMETRY=1
CMD ["--port", "44388"]
