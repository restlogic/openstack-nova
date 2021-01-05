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

import ddt
import mock
from oslo_utils.fixture import uuidsentinel

from nova.compute import vm_states
from nova import exception
from nova import objects
from nova import test
from nova.virt.libvirt import machine_type_utils


@ddt.ddt
class TestMachineTypeUtils(test.NoDBTestCase):

    def _create_test_instance_obj(
        self,
        vm_state=vm_states.STOPPED,
        mtype=None
    ):
        instance = objects.Instance(
            uuid=uuidsentinel.instance, host='fake', node='fake',
            task_state=None, flavor=objects.Flavor(),
            project_id='fake-project', user_id='fake-user',
            vm_state=vm_state, system_metadata={}
        )
        if mtype:
            instance.system_metadata = {
                'image_hw_machine_type': mtype,
            }
        return instance

    @mock.patch('nova.objects.Instance.get_by_uuid')
    @mock.patch('nova.context.target_cell')
    @mock.patch('nova.objects.InstanceMapping.get_by_instance_uuid',
        new=mock.Mock(cell_mapping=mock.sentinel.cell_mapping))
    def test_get_machine_type(self, mock_target_cell, mock_get_instance):
        mock_target_cell.return_value.__enter__.return_value = (
            mock.sentinel.cell_context)
        mock_get_instance.return_value = self._create_test_instance_obj(
            mtype='pc'
        )
        self.assertEqual(
            'pc',
            machine_type_utils.get_machine_type(
                mock.sentinel.context,
                instance_uuid=uuidsentinel.instance
            )
        )
        mock_get_instance.assert_called_once_with(
            mock.sentinel.cell_context,
            uuidsentinel.instance,
            expected_attrs=['system_metadata']
        )

    @mock.patch('nova.objects.Instance.get_by_uuid')
    @mock.patch('nova.context.target_cell')
    @mock.patch('nova.objects.InstanceMapping.get_by_instance_uuid',
        new=mock.Mock(cell_mapping=mock.sentinel.cell_mapping))
    def test_get_machine_type_none_found(
        self, mock_target_cell, mock_get_instance
    ):
        mock_target_cell.return_value.__enter__.return_value = (
            mock.sentinel.cell_context)
        mock_get_instance.return_value = self._create_test_instance_obj()
        self.assertIsNone(
            machine_type_utils.get_machine_type(
                mock.sentinel.context,
                instance_uuid=uuidsentinel.instance
            )
        )
        mock_get_instance.assert_called_once_with(
            mock.sentinel.cell_context,
            uuidsentinel.instance,
            expected_attrs=['system_metadata']
        )

    @ddt.data(
        'pc',
        'q35',
        'virt',
        's390-ccw-virtio',
        'pc-i440fx-2.12',
        'pc-q35-2.12',
        'virt-2.12',
        'pc-i440fx-rhel8.2.0',
        'pc-q35-rhel8.2.0')
    def test_check_machine_type_support(self, machine_type):
        # Assert UnsupportedMachineType isn't raised for supported types
        machine_type_utils._check_machine_type_support(
            machine_type)

    @ddt.data(
        'pc-foo',
        'pc-foo-1.2',
        'bar-q35',
        'virt-foo',
        'pc-virt')
    def test_check_machine_type_support_failure(self, machine_type):
        # Assert UnsupportedMachineType is raised for unsupported types
        self.assertRaises(
            exception.UnsupportedMachineType,
            machine_type_utils._check_machine_type_support,
            machine_type
        )

    @ddt.data(
        ('pc-i440fx-2.10', 'pc-i440fx-2.11'),
        ('pc-q35-2.10', 'pc-q35-2.11'))
    def test_check_update_to_existing_type(self, machine_types):
        # Assert that exception.InvalidMachineTypeUpdate is not raised when
        # updating to the same type or between versions of the same type
        original_type, update_type = machine_types
        machine_type_utils._check_update_to_existing_type(
            original_type, update_type)

    @ddt.data(
        ('pc', 'q35'),
        ('q35', 'pc'),
        ('pc-i440fx-2.12', 'pc-q35-2.12'),
        ('pc', 'pc-i440fx-2.12'),
        ('pc-i440fx-2.12', 'pc'),
        ('pc-i440fx-2.12', 'pc-i440fx-2.11'))
    def test_check_update_to_existing_type_failure(self, machine_types):
        # Assert that exception.InvalidMachineTypeUpdate is raised when
        # updating to a different underlying machine type or between versioned
        # and aliased machine types
        existing_type, update_type = machine_types
        self.assertRaises(
            exception.InvalidMachineTypeUpdate,
            machine_type_utils._check_update_to_existing_type,
            existing_type, update_type
        )

    @ddt.data(
        vm_states.STOPPED,
        vm_states.SHELVED,
        vm_states.SHELVED_OFFLOADED)
    def test_check_vm_state(self, vm_state):
        instance = self._create_test_instance_obj(
            vm_state=vm_state
        )
        machine_type_utils._check_vm_state(instance)

    @ddt.data(
        vm_states.ACTIVE,
        vm_states.PAUSED,
        vm_states.ERROR)
    def test_check_vm_state_failure(self, vm_state):
        instance = self._create_test_instance_obj(
            vm_state=vm_state
        )
        self.assertRaises(
            exception.InstanceInvalidState,
            machine_type_utils._check_vm_state,
            instance
        )

    @mock.patch('nova.objects.instance.Instance.save')
    @mock.patch('nova.virt.libvirt.machine_type_utils._check_vm_state')
    @mock.patch('nova.objects.Instance.get_by_uuid')
    @mock.patch('nova.context.target_cell')
    @mock.patch('nova.objects.InstanceMapping.get_by_instance_uuid',
                new=mock.Mock(cell_mapping=mock.sentinel.cell_mapping))
    def test_update_noop(
        self,
        mock_target_cell,
        mock_get_instance,
        mock_check_vm_state,
        mock_instance_save
    ):
        # Assert that update_machine_type is a noop when the type is already
        # set within the instance, even if forced
        existing_type = 'pc'
        mock_target_cell.return_value.__enter__.return_value = (
            mock.sentinel.cell_context)
        mock_get_instance.return_value = self._create_test_instance_obj(
            mtype=existing_type,
        )

        self.assertEqual(
            (existing_type, existing_type),
            machine_type_utils.update_machine_type(
                mock.sentinel.context,
                instance_uuid=uuidsentinel.instance,
                machine_type=existing_type
            ),
        )
        mock_check_vm_state.assert_not_called()
        mock_instance_save.assert_not_called()

        self.assertEqual(
            (existing_type, existing_type),
            machine_type_utils.update_machine_type(
                mock.sentinel.context,
                instance_uuid=uuidsentinel.instance,
                machine_type=existing_type,
                force=True
            ),
        )
        mock_check_vm_state.assert_not_called()
        mock_instance_save.assert_not_called()

    @ddt.data(
        ('foobar', 'foobar', None),
        ('foobar-1.3', 'foobar-1.3', 'foobar-1.2'),
        ('foobar-1.2', 'foobar-1.2', 'foobar-1.3'),
        ('foobar', 'foobar', 'q35'),
        ('pc', 'pc', 'q35'))
    @mock.patch('nova.objects.instance.Instance.save')
    @mock.patch('nova.objects.Instance.get_by_uuid')
    @mock.patch('nova.context.target_cell')
    @mock.patch('nova.objects.InstanceMapping.get_by_instance_uuid',
                new=mock.Mock(cell_mapping=mock.sentinel.cell_mapping))
    def test_update_force(
        self,
        types,
        mock_target_cell,
        mock_get_instance,
        mock_instance_save
    ):
        expected_type, update_type, existing_type = types
        mock_target_cell.return_value.__enter__.return_value = (
            mock.sentinel.cell_context)
        instance = self._create_test_instance_obj(
            mtype=existing_type
        )
        mock_get_instance.return_value = instance

        returned_type = machine_type_utils.update_machine_type(
            mock.sentinel.context,
            uuidsentinel.instance,
            machine_type=update_type,
            force=True
        )

        # Assert that the instance machine type was updated and saved
        self.assertEqual((expected_type, existing_type), returned_type)
        self.assertEqual(
            expected_type,
            instance.system_metadata.get('image_hw_machine_type')
        )
        mock_instance_save.assert_called_once()

    @ddt.data(
        ('pc', 'pc', None),
        ('q35', 'q35', None),
        ('pc-1.2', 'pc-1.2', None),
        ('pc-q35-1.2', 'pc-q35-1.2', None),
        ('pc-1.2', 'pc-1.2', 'pc-1.1'),
        ('pc-i440fx-1.2', 'pc-i440fx-1.2', 'pc-i440fx-1.1'),
        ('pc-q35-1.2', 'pc-q35-1.2', 'pc-q35-1.1'),
        ('pc-q35-rhel8.2.0', 'pc-q35-rhel8.2.0', 'pc-q35-rhel8.1.0'))
    @mock.patch('nova.objects.instance.Instance.save')
    @mock.patch('nova.objects.Instance.get_by_uuid')
    @mock.patch('nova.context.target_cell')
    @mock.patch('nova.objects.InstanceMapping.get_by_instance_uuid',
                new=mock.Mock(cell_mapping=mock.sentinel.cell_mapping))
    def test_update(
        self,
        types,
        mock_target_cell,
        mock_get_instance,
        mock_instance_save
    ):
        expected_type, update_type, existing_type = types
        mock_target_cell.return_value.__enter__.return_value = (
            mock.sentinel.cell_context)
        instance = self._create_test_instance_obj(
            mtype=existing_type
        )
        mock_get_instance.return_value = instance

        returned_type = machine_type_utils.update_machine_type(
            mock.sentinel.context,
            uuidsentinel.instance,
            machine_type=update_type
        )

        # Assert that the instance machine type was updated and saved
        self.assertEqual((expected_type, existing_type), returned_type)
        self.assertEqual(
            expected_type,
            instance.system_metadata.get('image_hw_machine_type')
        )
        mock_instance_save.assert_called_once()
