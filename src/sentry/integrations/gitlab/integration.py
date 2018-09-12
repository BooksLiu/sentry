from __future__ import absolute_import

from six.moves.urllib.parse import urlparse
from django.utils.translation import ugettext_lazy as _
from django import forms

from sentry import http
from sentry.web.helpers import render_to_response
from sentry.identity.pipeline import IdentityProviderPipeline
from sentry.identity.gitlab import get_user_info
from sentry.identity.gitlab.provider import GitlabIdentityProvider
from sentry.integrations import IntegrationInstallation, IntegrationFeatures, IntegrationProvider, IntegrationMetadata
from sentry.pipeline import NestedPipelineView, PipelineView
from sentry.utils.http import absolute_uri
from sentry.integrations.constants import ERR_INTERNAL, ERR_UNAUTHORIZED
from sentry.integrations.exceptions import ApiError


DESCRIPTION = """
Fill me out
"""

metadata = IntegrationMetadata(
    description=DESCRIPTION.strip(),
    author='The Sentry Team',
    noun=_('Installation'),
    issue_url='https://github.com/getsentry/sentry/issues/',
    source_url='https://github.com/getsentry/sentry/tree/master/src/sentry/integrations/gitlab',
    aspects={},
)


API_ERRORS = {
    404: 'Gitlab returned a 404 Not Found error. If this repository exists, ensure'
         ' that your installation has permission to access this repository',
    # ' (https://github.com/settings/installations).',
    401: ERR_UNAUTHORIZED,
}


class GitlabIntegration(IntegrationInstallation):

    def get_client(self):
        pass

    def reinstall(self):
        installation_id = self.model.external_id.split(':')[1]
        metadata = self.model.metadata
        metadata['installation_id'] = installation_id
        self.model.update(metadata=metadata)
        self.reinstall_repositories()

    def search_issues(self, query):
        pass

    def message_from_error(self, exc):
        if isinstance(exc, ApiError):
            message = API_ERRORS.get(exc.code)
            if message:
                return message
            return (
                'Error Communicating with Gitlab (HTTP %s): %s' % (
                    exc.code, exc.json.get('message', 'unknown error')
                    if exc.json else 'unknown error',
                )
            )
        else:
            return ERR_INTERNAL


class InstallationForm(forms.Form):
    url = forms.CharField(
        label=_("Installation Url"),
        help_text=_('The "base URL" for your gitlab instance, '
                    'includes the host and protocol.'),
        widget=forms.TextInput(
            attrs={'placeholder': 'https://github.example.com'}
        ),
    )
    name = forms.CharField(
        label=_("Gitlab App Name"),
        help_text=_('The name of your OAuth Application in Gitlab. '
                    'This can be found on the apps configuration '
                    'page. (/profile/applications)'),
        widget=forms.TextInput(
            attrs={'placeholder': _('Sentry App')}
        )
    )
    group = forms.CharField(
        label=_("Gitlab Group Name"),
        widget=forms.TextInput(
            attrs={'placeholder': _('my-awesome-group')}
        )
    )
    verify_ssl = forms.BooleanField(
        label=_("Verify SSL"),
        help_text=_('By default, we verify SSL certificates '
                    'when delivering payloads to your Gitlab instance'),
        widget=forms.CheckboxInput(),
        required=False
    )
    client_id = forms.CharField(
        label=_("Gitlab Application ID"),
        widget=forms.TextInput(
            attrs={'placeholder': _(
                '5832fc6e14300a0d962240a8144466eef4ee93ef0d218477e55f11cf12fc3737')}
        )
    )
    client_secret = forms.CharField(
        label=_("Gitlab Application Secret"),
        widget=forms.TextInput(
            attrs={'placeholder': _('XXXXXXXXXXXXXXXXXXXXXXXXXXX')}
        )
    )

    def __init__(self, *args, **kwargs):
        super(InstallationForm, self).__init__(*args, **kwargs)
        self.fields['verify_ssl'].initial = True


class InstallationConfigView(PipelineView):
    def dispatch(self, request, pipeline):
        form = InstallationForm(request.POST)
        if form.is_valid():
            form_data = form.cleaned_data
            form_data['url'] = urlparse(form_data['url']).netloc

            pipeline.bind_state('installation_data', form_data)

            pipeline.bind_state('oauth_config_information', {
                "access_token_url": u"https://{}/oauth/token".format(form_data.get('url')),
                "authorize_url": u"https://{}/oauth/authorize".format(form_data.get('url')),
                "client_id": form_data.get('client_id'),
                "client_secret": form_data.get('client_secret'),
                "verify_ssl": form_data.get('verify_ssl')
            })

            return pipeline.next_step()

        project_form = InstallationForm()

        return render_to_response(
            template='sentry/integrations/gitlab-config.html',
            context={
                'form': project_form,
            },
            request=request,
        )


class GitlabIntegrationProvider(IntegrationProvider):
    key = 'gitlab'
    name = 'Gitlab'
    metadata = metadata
    integration_cls = GitlabIntegration

    needs_default_identity = True

    features = frozenset([
        IntegrationFeatures.ISSUE_BASIC,
    ])

    setup_dialog_config = {
        'width': 1030,
        'height': 1000,
    }

    def _make_identity_pipeline_view(self):
        """
        Make the nested identity provider view. It is important that this view is
        not constructed until we reach this step and the
        ``oauth_config_information`` is available in the pipeline state. This
        method should be late bound into the pipeline vies.
        """
        identity_pipeline_config = dict(
            oauth_scopes=(
                'api',
                'sudo',
            ),
            redirect_url=absolute_uri('/extensions/gitlab/setup/'),
            **self.pipeline.fetch_state('oauth_config_information')
        )

        return NestedPipelineView(
            bind_key='identity',
            provider_key='gitlab',
            pipeline_cls=IdentityProviderPipeline,
            config=identity_pipeline_config,
        )

    def get_oauth_data(self, payload):
        data = {'access_token': payload['access_token']}

        # https://docs.gitlab.com/ee/api/oauth2.html#2-requesting-access-token
        # doesn't seem to be correct, format we actually get:
        # {
        #   "access_token": "123432sfh29uhs29347",
        #   "token_type": "bearer",
        #   "refresh_token": "29f43sdfsk22fsj929",
        #   "created_at": 1536798907,
        #   "scope": "api sudo"
        # }
        if 'refresh_token' in payload:
            data['refresh_token'] = payload['refresh_token']
        if 'token_type' in payload:
            data['token_type'] = payload['token_type']

        return data

    def get_group_info(self, access_token, installation_data):
        session = http.build_session()
        resp = session.get(
            u'https://{}/api/v4/groups/{}'.format(
                installation_data['url'], installation_data['group']),
            headers={
                'Accept': 'application/json',
                'Authorization': 'Bearer %s' % access_token,
            },
            verify=installation_data['verify_ssl']
        )

        resp.raise_for_status()
        return resp.json()

    def get_pipeline_views(self):
        return [InstallationConfigView(), lambda: self._make_identity_pipeline_view()]

    def build_integration(self, state):
        data = state['identity']['data']
        oauth_data = self.get_oauth_data(data)
        user = get_user_info(data['access_token'], state['installation_data'])
        group = self.get_group_info(data['access_token'], state['installation_data'])
        scopes = sorted(GitlabIdentityProvider.oauth_scopes)
        base_url = state['installation_data']['url']

        integration = {
            'name': group['name'],
            'external_id': u'{}:{}'.format(base_url, group['id']),
            'metadata': {
                'icon': group['avatar_url'],
                'domain_name': group['web_url'].replace('https://', ''),
                'scopes': scopes,
                'verify_ssl': state['installation_data']['verify_ssl'],
            },
            'user_identity': {
                'type': 'gitlab',
                'external_id': user['id'],
                'scopes': scopes,
                'data': oauth_data,
            },
        }

        return integration