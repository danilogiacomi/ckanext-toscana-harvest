import urllib
import http

from ckan.lib.base import c
from ckan import model
from ckan.model import Session, Package
from ckan.logic import ValidationError, NotFound, get_action
from ckan.lib.helpers import json
from ckan.lib.munge import munge_name
from simplejson.scanner import JSONDecodeError

from ckanext.harvest.harvesters.ckanharvester import CKANHarvester
from ckanext.harvest.harvesters.base import HarvesterBase
from ckanext.harvest.model import HarvestJob, HarvestObject, HarvestGatherError, \
                                    HarvestObjectError

import logging
log = logging.getLogger(__name__)

class SpodHarvester(HarvesterBase):
    '''
    A Harvester for Spod instances
    '''
    config = None

    api_version = 2
    action_api_version = 3

    def _get_rest_api_offset(self):
        return '/api/%d/rest' % self.api_version

    def _get_action_api_offset(self):
        return '/api/%d/action' % self.action_api_version

    def _get_search_api_offset(self):
        return '/api/%d/search' % self.api_version

    def _get_content(self, url):
        log.error("get_content " + url)
        http_request = urllib.request.Request(url=url)

        api_key = self.config.get('api_key')
        if api_key:
            http_request.add_header('Authorization', api_key)

        try:
            http_response = urllib.request.urlopen(http_request)
        except urllib.error.HTTPError as e:
            log.error("httperror")
            log.error(e.getcode())
            log.error(e.reason)
            if e.getcode() == 404:
                raise ContentNotFoundError('HTTP error: %s' % e.code)
            else:
                raise ContentFetchError('HTTP error: %s' % e.code)
        except urllib.error.URLError as  e:
            log.error("urlerror")
            log.error(e.getcode())
            log.error(e.reason)
            raise ContentFetchError('URL error: %s' % e.reason)
        except http.client.HTTPException as e:
            log.error("httpexcption")
            log.error(e.getcode())
            log.error(e.reason)
            raise ContentFetchError('HTTP Exception: %s' % e)
        return http_response.read()

    def _get_group(self, base_url, group_name):
        url = base_url + self._get_rest_api_offset() + '/group/' + munge_name(group_name)
        try:
            content = self._get_content(url)
            return json.loads(content)
        except (ContentFetchError, ValueError):
            log.error('Could not fetch/decode remote group');
            raise RemoteResourceError('Could not fetch/decode remote group')

    def _get_organization(self, base_url, org_name):
        url = base_url + self._get_action_api_offset() + '/organization_show?id=' + org_name
        try:
            content = self._get_content(url)
            content_dict = json.loads(content)
            return content_dict['result']
        except (ContentFetchError, ValueError, KeyError):
            log.error('Could not fetch/decode remote group');
            raise RemoteResourceError('Could not fetch/decode remote organization')

    def _set_config(self,config_str):
        if config_str:
            self.config = json.loads(config_str)
            if 'api_version' in self.config:
                self.api_version = int(self.config['api_version'])

            log.debug('Using config: %r', self.config)
        else:
            self.config = {}

    def info(self):
        return {
            'name': 'Spod',
            'title': 'Spod',
            'description': 'Harvests remote Spod instances',
            'form_config_interface':'Text'
        }

    def validate_config(self,config):
        if not config:
            return config

        try:
            config_obj = json.loads(config)

            if 'api_version' in config_obj:
                try:
                    int(config_obj['api_version'])
                except ValueError:
                    raise ValueError('api_version must be an integer')

            if 'default_tags' in config_obj:
                if not isinstance(config_obj['default_tags'],list):
                    raise ValueError('default_tags must be a list')

            if 'default_groups' in config_obj:
                if not isinstance(config_obj['default_groups'],list):
                    raise ValueError('default_groups must be a list')

                # Check if default groups exist
                context = {'model':model,'user':c.user}
                for group_name in config_obj['default_groups']:
                    try:
                        group = get_action('group_show')(context,{'id':group_name})
                    except NotFound as e:
                        raise ValueError('Default group not found')

            if 'default_extras' in config_obj:
                if not isinstance(config_obj['default_extras'],dict):
                    raise ValueError('default_extras must be a dictionary')

            if 'user' in config_obj:
                # Check if user exists
                context = {'model':model,'user':c.user}
                try:
                    user = get_action('user_show')(context,{'id':config_obj.get('user')})
                except NotFound as e:
                    raise ValueError('User not found')

            for key in ('read_only','force_all'):
                if key in config_obj:
                    if not isinstance(config_obj[key],bool):
                        raise ValueError('%s must be boolean' % key)

        except ValueError as e:
            raise e

        return config


    def gather_stage(self,harvest_job):
        log.error('In SpodHarvester gather_stage (%s)' % harvest_job.source.url)
        get_all_packages = True
        package_ids = []

        self._set_config(harvest_job.source.config)

        # Check if this source has been harvested before
        previous_job = Session.query(HarvestJob) \
                        .filter(HarvestJob.source==harvest_job.source) \
                        .filter(HarvestJob.gather_finished!=None) \
                        .filter(HarvestJob.id!=harvest_job.id) \
                        .order_by(HarvestJob.gather_finished.desc()) \
                        .limit(1).first()

        # Get source URL
        base_url = harvest_job.source.url.rstrip('/')
        base_rest_url = base_url + self._get_rest_api_offset()
        base_search_url = base_url + self._get_search_api_offset()

        # Filter in/out datasets from particular organizations
        org_filter_include = self.config.get('organizations_filter_include', [])
        org_filter_exclude = self.config.get('organizations_filter_exclude', [])
        def get_pkg_ids_for_organizations(orgs):
            pkg_ids = set()
            for organization in orgs:
                url = base_search_url + '/dataset?organization=%s' % organization
                content = self._get_content(url)
                content_json = json.loads(content)
                result_count = int(content_json['count'])
                pkg_ids |= set(content_json['results'])
                while len(pkg_ids) < result_count or not content_json['results']:
                    url = base_search_url + '/dataset?organization=%s&offset=%s' % (organization, len(pkg_ids))
                    content = self._get_content(url)
                    content_json = json.loads(content)
                    pkg_ids |= set(content_json['results'])
            return pkg_ids
        include_pkg_ids = get_pkg_ids_for_organizations(org_filter_include)
        exclude_pkg_ids = get_pkg_ids_for_organizations(org_filter_exclude)

        if (previous_job and not previous_job.gather_errors and not len(previous_job.objects) == 0):
            if not self.config.get('force_all',False):
                get_all_packages = True

        if get_all_packages:
            # Request all remote packages
            log.error("Request all remote packages")
            url = base_rest_url + '/package'
            log.error(url)
            try:
                content = self._get_content(url)
                package_ids = json.loads(content)
            except ContentFetchError as e:
                log.error("Unable to get content for URL")
                self._save_gather_error('Unable to get content for URL: %s: %s' % (url, str(e)),harvest_job)
                return None
            except JSONDecodeError as e:
                log.error("Unable to decode content for URL")
                self._save_gather_error('Unable to decode content for URL: %s: %s' % (url, str(e)),harvest_job)
                return None

        if org_filter_include:
            package_ids = set(package_ids) & include_pkg_ids
        elif org_filter_exclude:
            package_ids = set(package_ids) - exclude_pkg_ids

        try:
            object_ids = []
            if len(package_ids):
                for package_id in package_ids:
                    # Create a new HarvestObject for this identifier
                    obj = HarvestObject(guid = package_id, job = harvest_job)
                    obj.save()
                    object_ids.append(obj.id)

                return object_ids

            else:
                self._save_gather_error('No packages received for URL: %s' % url,
                       harvest_job)
                log.error("No packages received for URL")
                return None
        except Exception as e:
            log.error(e.message)
            self._save_gather_error('%r'%e.message,harvest_job)


    def fetch_stage(self,harvest_object):
        log.error('In SpodHarvester fetch_stage')

        self._set_config(harvest_object.job.source.config)

        # Get source URL
        url = harvest_object.source.url.rstrip('/')
        url = url + self._get_rest_api_offset() + '/package/' + harvest_object.guid

        # Get contents
        try:
            content = self._get_content(url)
        except ContentFetchError as e:
            log.error('Unable to get content for package: %s: %r' % (url, e))
            self._save_object_error('Unable to get content for package: %s: %r' % \
                                        (url, e),harvest_object)
            return None

        # Save the fetched contents in the HarvestObject
        harvest_object.content = content
        harvest_object.save()
        return True

    def import_stage(self,harvest_object):
        log.error('In SpodHarvester import_stage')

        context = {'model': model, 'session': Session, 'user': self._get_user_name()}
        if not harvest_object:
            log.error('No harvest object received')
            return False

        if harvest_object.content is None:
            self._save_object_error('Empty content for object %s' % harvest_object.id,
                    harvest_object, 'Import')
            return False

        self._set_config(harvest_object.job.source.config)

        try:
            package_dict = json.loads(harvest_object.content)

            if package_dict.get('type') == 'harvest':
                log.warn('Remote dataset is a harvest source, ignoring...')
                return True

            # Set default tags if needed
            default_tags = self.config.get('default_tags',[])
            if default_tags:
                if not 'tags' in package_dict:
                    package_dict['tags'] = []
                package_dict['tags'].extend([t for t in default_tags if t not in package_dict['tags']])

            remote_groups = self.config.get('remote_groups', None)
            if not remote_groups in ('only_local', 'create'):
                # Ignore remote groups
                package_dict.pop('groups', None)
            else:
                if not 'groups' in package_dict:
                    package_dict['groups'] = []

                # check if remote groups exist locally, otherwise remove
                validated_groups = []

                for group_name in package_dict['groups']:
                    try:
                        data_dict = {'id': group_name}
                        group = get_action('group_show')(context, data_dict)
                        if self.api_version == 1:
                            validated_groups.append(group['name'])
                        else:
                            validated_groups.append(group['id'])
                    except NotFound as e:
                        log.info('Group %s is not available' % group_name)
                        if remote_groups == 'create':
                            try:
                                group = self._get_group(harvest_object.source.url, group_name)
                            except RemoteResourceError:
                                log.error('Could not get remote group %s' % group_name)
                                continue

                            for key in ['packages', 'created', 'users', 'groups', 'tags', 'extras', 'display_name']:
                                group.pop(key, None)

                            get_action('group_create')(context, group)
                            log.info('Group %s has been newly created' % group_name)
                            if self.api_version == 1:
                                validated_groups.append(group['name'])
                            else:
                                validated_groups.append(group['id'])

                package_dict['groups'] = validated_groups


            # Local harvest source organization
            source_dataset = get_action('package_show')(context, {'id': harvest_object.source.id})
            local_org = source_dataset.get('owner_org')

            remote_orgs = self.config.get('remote_orgs', None)

            if not remote_orgs in ('only_local', 'create'):
                # Assign dataset to the source organization
                package_dict['owner_org'] = local_org
            else:
                if not 'owner_org' in package_dict:
                    package_dict['owner_org'] = None

                # check if remote org exist locally, otherwise remove
                validated_org = None
                remote_org = package_dict['owner_org']

                if remote_org:
                    try:
                        data_dict = {'id': remote_org}
                        org = get_action('organization_show')(context, data_dict)
                        validated_org = org['id']
                    except NotFound as e:
                        log.info('Organization %s is not available' % remote_org)
                        if remote_orgs == 'create':
                            try:
                                try:
                                    org = self._get_organization(harvest_object.source.url, remote_org)
                                except RemoteResourceError:
                                    # fallback if remote Spod exposes organizations as groups
                                    # this especially targets older versions of Spod
                                    org = self._get_group(harvest_object.source.url, remote_org)

                                for key in ['packages', 'created', 'users', 'groups', 'tags', 'extras', 'display_name', 'type']:
                                    org.pop(key, None)
                                get_action('organization_create')(context, org)
                                log.info('Organization %s has been newly created' % remote_org)
                                validated_org = org['id']
                            except (RemoteResourceError, ValidationError):
                                log.error('Could not get remote org %s' % remote_org)

                package_dict['owner_org'] = validated_org or local_org

            # Set default groups if needed
            default_groups = self.config.get('default_groups', [])
            if default_groups:
                if not 'groups' in package_dict:
                    package_dict['groups'] = []
                package_dict['groups'].extend([g for g in default_groups if g not in package_dict['groups']])

            # Find any extras whose values are not strings and try to convert
            # them to strings, as non-string extras are not allowed anymore in
            # CKAN 2.0.
            for key in package_dict['extras'].keys():
                if not isinstance(package_dict['extras'][key], basestring):
                    try:
                        package_dict['extras'][key] = json.dumps(
                                package_dict['extras'][key])
                    except TypeError:
                        # If converting to a string fails, just delete it.
                        del package_dict['extras'][key]

            # Set default extras if needed
            default_extras = self.config.get('default_extras',{})
            if default_extras:
                override_extras = self.config.get('override_extras',False)
                if not 'extras' in package_dict:
                    package_dict['extras'] = {}
                for key,value in default_extras.items():
                    if not key in package_dict['extras'] or override_extras:
                        # Look for replacement strings
                        if isinstance(value,basestring):
                            value = value.format(harvest_source_id=harvest_object.job.source.id,
                                     harvest_source_url=harvest_object.job.source.url.strip('/'),
                                     harvest_source_title=harvest_object.job.source.title,
                                     harvest_job_id=harvest_object.job.id,
                                     harvest_object_id=harvest_object.id,
                                     dataset_id=package_dict['id'])

                        package_dict['extras'][key] = value

            for resource in package_dict.get('resources', []):
                # Clear remote url_type for resources (eg datastore, upload) as
                # we are only creating normal resources with links to the
                # remote ones
                resource.pop('url_type', None)

                # Clear revision_id as the revision won't exist on this CKAN
                # and saving it will cause an IntegrityError with the foreign
                # key.
                resource.pop('revision_id', None)

            result = self._create_or_update_package(package_dict,harvest_object)
            return result
        except ValidationError as e:
            self._save_object_error('Invalid package with GUID %s: %r' % (harvest_object.guid, e.error_dict),
                    harvest_object, 'Import')
        except Exception as e:
            self._save_object_error('%r'%e,harvest_object,'Import')

class ContentFetchError(Exception):
    pass

class ContentNotFoundError(ContentFetchError):
    pass

class RemoteResourceError(Exception):
    pass
