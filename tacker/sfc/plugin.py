# vim: tabstop=4 shiftwidth=4 softtabstop=4
#
# All Rights Reserved.
#
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.
#
# @author: Tim Rozet, Red Hat Inc.


import copy
import eventlet
import inspect

from oslo_config import cfg
from sqlalchemy.orm import exc as orm_exc

from tacker.api.v1 import attributes
from tacker.common import driver_manager
from tacker import context as t_context
from tacker.db.sfc import sfc_db
from tacker.extensions import sfc
from tacker.openstack.common import excutils
from tacker.openstack.common import log as logging
from tacker.plugins.common import constants
from neutronclient.v2_0 import client as neutron_client
from tacker.db.vm.vm_db import VNFMPluginDb
from tacker import manager
import json
import re

LOG = logging.getLogger(__name__)


class SFCPlugin(sfc_db.SFCPluginDb):
    """SFCPlugin which supports SFC framework
    """
    OPTS = [
        cfg.ListOpt(
            'infra_driver', default=['opendaylight'],
            help=_('Hosting device drivers sfc plugin will use')),
    ]
    cfg.CONF.register_opts(OPTS, 'sfc')
    supported_extension_aliases = ['sfc']

    def __init__(self):
        super(SFCPlugin, self).__init__()
        self._pool = eventlet.GreenPool()
        self._device_manager = driver_manager.DriverManager(
            'tacker.sfc.drivers',
            cfg.CONF.sfc.infra_driver)

    def spawn_n(self, function, *args, **kwargs):
        self._pool.spawn_n(function, *args, **kwargs)

    def _find_vnf_info(self, context, chain):
        sfc_dict = dict()
        for vnf in chain:
            vnfm_plugin = manager.TackerManager.get_service_plugins()['VNFM']
            vnf_data = vnfm_plugin.get_vnf(context, vnf)
            sfc_dict[vnf] = dict()
            # find IP in mgmt_url string
            sfc_dict[vnf]['ip'] = re.search(r'[0-9]+(?:\.[0-9]+){3}', vnf_data['mgmt_url']).group()
            # trozet check here to see how services are passed
            # we can only specify 1 atm for ODL
            sfc_dict[vnf]['type'] = vnf_data['attributes']['service_type']
            sfc_dict[vnf]['name'] = vnf_data['name']
            # we also need the neutron port ID
            # tacker doesnt find this so we can use the vnf id to find the
            # neutron port as it is listed in the name
            nc = NeutronClient()
            port_output = nc.list_neutron_ports()
            for port in port_output['ports']:
                if port['name'].find(vnf_data['id']) > 0:
                    sfc_dict[vnf]['neutron_port_id'] = port['id']
            if 'neutron_port_id' not in sfc_dict[vnf]:
                raise KeyError('Unable to find neutron_port_id')

        return sfc_dict

    def _create_sfc(self, context, sfcd):
        """
        :param context:
        :param sfcd: dictionary of kwargs from REST request
        :return: dictionary of created object?
        """
        sfc_dict = sfcd['sfc']
        LOG.debug(_('chain_dict %s'), sfc_dict)
        vnf_dict = self._find_vnf_info(context, sfc_dict['chain'])
        LOG.debug(_('VNF DICT: %s'), vnf_dict)
        sfc_dict = self._create_sfc_pre(context, sfcd)
        LOG.debug(_('sfc_dict after database entry %s'), sfc_dict)

        sfc_id = sfc_dict['id']
        # Default driver for SFC is opendaylight
        # when other drivers are available switch on them here
        if 'infra_driver' not in sfc_dict:
            infra_driver = 'opendaylight'
        else:
            infra_driver = sfc_dict['infra_driver']

        try:
            instance_id = self._device_manager.invoke(
                infra_driver, 'create_sfc', sfc_dict=sfc_dict, vnf_dict=vnf_dict)
        except sfc.SFCCreateFailed:
            LOG.exception(_('Unable to create SFC'))
            return

        if instance_id is None:
            self._create_sfc_post(context, sfc_id, None, sfc_dict)
            return sfc_dict

        sfc_dict['instance_id'] = instance_id
        LOG.debug(_('sfc_dict after sfc SFC Create complete: %s'), sfc_dict)
        return sfc_dict

    def create_sfc(self, context, sfc):
        sfc_dict = self._create_sfc(context, sfc)

        def create_sfc_wait():
            self._create_sfc_wait(context, sfc_dict)

        self.spawn_n(create_sfc_wait)
        return sfc_dict

    def _create_sfc_wait(self, context, sfc_dict):
        driver_name = self._infra_driver_name(sfc_dict)
        sfc_id = sfc_dict['id']
        instance_id = self._instance_id(sfc_dict)

        try:
            self._device_manager.invoke(
                driver_name, 'create_wait', plugin=self, context=context,
                sfc_dict=sfc_dict, sfc_id=instance_id)
        except sfc.SFCCreateWaitFailed:
            instance_id = None
            del sfc_dict['instance_id']

        self._create_sfc_post(
            context, sfc_id, instance_id, sfc_dict)
        if instance_id is None:
            return

        new_status = constants.ACTIVE

        sfc_dict['status'] = new_status
        self._create_sfc_status(context, sfc_id, new_status)

    # TODO fill in update and delete


class NeutronClient:
    def __init__(self):
        auth_url = cfg.CONF.keystone_authtoken.auth_uri + '/v2.0'
        authtoken = cfg.CONF.keystone_authtoken
        kwargs = {
            'password': authtoken.password,
            'tenant_name': authtoken.project_name,
            'username': authtoken.username,
            'auth_url': auth_url,
        }
        self.client = neutron_client.Client(**kwargs)

    def list_neutron_ports(self):
        LOG.debug("list_ports()", )
        port_info = self.client.list_ports()
        print "API.Tacker Neutron Port List::" + str(port_info)
        return port_info
