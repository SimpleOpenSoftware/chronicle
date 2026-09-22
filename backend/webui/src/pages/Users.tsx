import React, { useState } from 'react'
import { Users as UsersIcon, Plus, Edit, Trash2, RefreshCw, Shield, User, Mail, ChevronRight, ChevronDown, Key, Smartphone } from 'lucide-react'
import ApiKeysPanel from '../components/ApiKeysPanel'
import { useAuth } from '../contexts/AuthContext'
import { useUsers, useCreateUser, useUpdateUser, useDeleteUser } from '../hooks/useUsers'
import { Button, IconButton, Input, Label, Checkbox, StateBadge } from '../components/ui'

interface User {
  _id: string
  display_name: string | null
  email: string
  is_superuser: boolean
  is_active: boolean
  is_verified: boolean
  device_count: number
}

interface UserFormData {
  display_name: string
  email: string
  password: string
  is_superuser: boolean
  is_active: boolean
}

export default function Users() {
  const [showCreateForm, setShowCreateForm] = useState(false)
  const [editingUser, setEditingUser] = useState<User | null>(null)
  // Which users' API-key panels are expanded. Keys load lazily per user, so
  // an admin with many users doesn't fan out a request per row on page load.
  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  const [actionError, setActionError] = useState<string | null>(null)

  const { isAdmin } = useAuth()
  const { data: users = [], isLoading: loading, error: queryError, refetch } = useUsers()
  const createUser = useCreateUser()
  const updateUser = useUpdateUser()
  const deleteUser = useDeleteUser()

  const error = queryError?.message ?? actionError ?? null

  const [formData, setFormData] = useState<UserFormData>({
    display_name: '',
    email: '',
    password: '',
    is_superuser: false,
    is_active: true,
  })

  const resetForm = () => {
    setFormData({
      display_name: '',
      email: '',
      password: '',
      is_superuser: false,
      is_active: true,
    })
    setEditingUser(null)
    setShowCreateForm(false)
  }

  const handleCreateUser = async (e: React.FormEvent) => {
    e.preventDefault()
    try {
      await createUser.mutateAsync(formData)
      resetForm()
    } catch (err: any) {
      setActionError(err.message || 'Failed to create user')
    }
  }

  const handleUpdateUser = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!editingUser) return

    try {
      const updateData = { ...formData }
      if (!updateData.password) {
        delete (updateData as any).password
      }
      await updateUser.mutateAsync({ id: editingUser._id, userData: updateData })
      resetForm()
    } catch (err: any) {
      setActionError(err.message || 'Failed to update user')
    }
  }

  const handleDeleteUser = async (user: User) => {
    if (!confirm(`Are you sure you want to delete user "${user.display_name || user.email}"?`)) return

    try {
      await deleteUser.mutateAsync(user._id)
    } catch (err: any) {
      setActionError(err.message || 'Failed to delete user')
    }
  }

  const toggleExpanded = (userId: string) => {
    setExpanded(prev => {
      const next = new Set(prev)
      next.has(userId) ? next.delete(userId) : next.add(userId)
      return next
    })
  }

  const handleEditUser = (user: User) => {
    setFormData({
      display_name: user.display_name || '',
      email: user.email,
      password: '',
      is_superuser: user.is_superuser,
      is_active: user.is_active,
    })
    setEditingUser(user)
    setShowCreateForm(true)
  }


  if (!isAdmin) {
    return (
      <div className="text-center">
        <Shield className="h-12 w-12 mx-auto mb-4 text-gray-400" />
        <h2 className="text-xl font-semibold text-gray-900 dark:text-gray-100 mb-2">
          Access Restricted
        </h2>
        <p className="text-gray-600 dark:text-gray-400">
          You need administrator privileges to manage users.
        </p>
      </div>
    )
  }

  return (
    <div>
      {/* Header */}
      <div className="flex flex-col gap-3 sm:flex-row sm:justify-between sm:items-center mb-6">
        <div className="flex items-center space-x-2">
          <UsersIcon className="h-6 w-6 text-blue-600 flex-shrink-0" />
          <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">
            User Management
          </h1>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Button variant="secondary" size="md" onClick={() => refetch()} icon={<RefreshCw className="h-4 w-4" />}>Refresh</Button>
          <Button variant="primary" size="md" onClick={() => setShowCreateForm(true)} icon={<Plus className="h-4 w-4" />}>Add User</Button>
        </div>
      </div>

      {/* Error Message */}
      {error && (
        <div className="bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-md p-4 mb-6">
          <p className="text-sm text-red-700 dark:text-red-300">{error}</p>
          <button
            onClick={() => setActionError(null)}
            className="text-red-600 hover:text-red-800 text-sm underline mt-1"
          >
            Dismiss
          </button>
        </div>
      )}

      {/* Create/Edit Form */}
      {showCreateForm && (
        <div className="bg-gray-50 dark:bg-gray-700 rounded-lg p-6 border border-gray-200 dark:border-gray-600 mb-6">
          <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100 mb-4">
            {editingUser ? 'Edit User' : 'Create New User'}
          </h2>
          <form onSubmit={editingUser ? handleUpdateUser : handleCreateUser} className="space-y-4">
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              <div>
                <Label className="mb-2">
                  Name
                </Label>
                <Input
                  type="text"
                  required
                  value={formData.display_name}
                  onChange={(e) => setFormData({ ...formData, display_name: e.target.value })}
                />
              </div>
              <div>
                <Label className="mb-2">
                  Email
                </Label>
                <Input
                  type="email"
                  required
                  value={formData.email}
                  onChange={(e) => setFormData({ ...formData, email: e.target.value })}
                />
              </div>
            </div>

            <div>
              <Label className="mb-2">
                Password {editingUser && "(leave blank to keep current password)"}
              </Label>
              <Input
                type="password"
                required={!editingUser}
                value={formData.password}
                onChange={(e) => setFormData({ ...formData, password: e.target.value })}
              />
            </div>

            <div className="flex items-center space-x-6">
              <Checkbox
                label="Administrator"
                checked={formData.is_superuser}
                onChange={(e) => setFormData({ ...formData, is_superuser: e.target.checked })}
              />
              <Checkbox
                label="Active"
                checked={formData.is_active}
                onChange={(e) => setFormData({ ...formData, is_active: e.target.checked })}
              />
            </div>

            <div className="flex space-x-2">
              <Button type="submit" variant="primary" size="md">
                {editingUser ? 'Update User' : 'Create User'}
              </Button>
              <Button type="button" variant="secondary" size="md" onClick={resetForm}>
                Cancel
              </Button>
            </div>
          </form>
        </div>
      )}

      {/* Users Table */}
      {loading ? (
        <div className="flex items-center justify-center h-32">
          <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-blue-600"></div>
          <span className="ml-2 text-gray-600 dark:text-gray-400">Loading users...</span>
        </div>
      ) : (
        <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 overflow-x-auto">
          <table className="min-w-full divide-y divide-gray-200 dark:divide-gray-700">
            <thead className="bg-gray-50 dark:bg-gray-700">
              <tr>
                <th className="w-10 px-2 py-3"></th>
                <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-300 uppercase tracking-wider">
                  User
                </th>
                <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-300 uppercase tracking-wider">
                  Email
                </th>
                <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-300 uppercase tracking-wider">
                  Role
                </th>
                <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-300 uppercase tracking-wider">
                  Status
                </th>
                <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-300 uppercase tracking-wider">
                  Devices
                </th>
                <th className="px-6 py-3 text-right text-xs font-medium text-gray-500 dark:text-gray-300 uppercase tracking-wider">
                  Actions
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-200 dark:divide-gray-700">
              {users.map((user) => (
                <React.Fragment key={user._id}>
                <tr className="hover:bg-gray-50 dark:hover:bg-gray-700">
                  <td className="px-2 py-4">
                    <IconButton
                      label={expanded.has(user._id) ? 'Hide API keys' : 'Show API keys'}
                      onClick={() => toggleExpanded(user._id)}
                    >
                      {expanded.has(user._id)
                        ? <ChevronDown className="h-4 w-4" />
                        : <ChevronRight className="h-4 w-4" />}
                    </IconButton>
                  </td>
                  <td className="px-6 py-4 whitespace-nowrap">
                    <div className="flex items-center">
                      <User className="h-8 w-8 text-gray-400" />
                      <div className="ml-4">
                        <div className="text-sm font-medium text-gray-900 dark:text-gray-100">
                          {user.display_name || 'No name set'}
                        </div>
                        {!user.is_verified && (
                          <div className="text-xs text-gray-500 dark:text-gray-400">Unverified</div>
                        )}
                      </div>
                    </div>
                  </td>
                  <td className="px-6 py-4 whitespace-nowrap">
                    <div className="flex items-center">
                      <Mail className="h-4 w-4 text-gray-400 mr-2" />
                      <span className="text-sm text-gray-900 dark:text-gray-100">{user.email}</span>
                    </div>
                  </td>
                  <td className="px-6 py-4 whitespace-nowrap">
                    <div className="flex items-center">
                      {user.is_superuser && <Shield className="h-4 w-4 text-blue-600 mr-1" />}
                      <StateBadge tone={user.is_superuser ? 'info' : 'neutral'}>
                        {user.is_superuser ? 'Admin' : 'User'}
                      </StateBadge>
                    </div>
                  </td>
                  <td className="px-6 py-4 whitespace-nowrap">
                    <StateBadge tone={user.is_active ? 'success' : 'danger'}>
                      {user.is_active ? 'Active' : 'Inactive'}
                    </StateBadge>
                  </td>
                  <td className="px-6 py-4 whitespace-nowrap">
                    <div className="flex items-center text-sm text-gray-600 dark:text-gray-400">
                      <Smartphone className="h-4 w-4 text-gray-400 mr-2" />
                      {user.device_count}
                    </div>
                  </td>
                  <td className="px-6 py-4 whitespace-nowrap text-right text-sm font-medium">
                    <div className="flex justify-end space-x-2">
                      <IconButton label="Edit user" onClick={() => handleEditUser(user)}>
                        <Edit className="h-4 w-4" />
                      </IconButton>
                      <IconButton label="Delete user" danger onClick={() => handleDeleteUser(user)}>
                        <Trash2 className="h-4 w-4" />
                      </IconButton>
                    </div>
                  </td>
                </tr>
                {expanded.has(user._id) && (
                  <tr className="bg-gray-50 dark:bg-gray-900/40">
                    <td colSpan={7} className="px-6 py-4">
                      <div className="flex items-center mb-3">
                        <Key className="h-4 w-4 text-blue-600 mr-2" />
                        <h4 className="text-sm font-semibold text-gray-900 dark:text-gray-100">
                          API Keys
                        </h4>
                      </div>
                      <p className="text-xs text-gray-500 dark:text-gray-400 mb-3">
                        Keys authenticate as {user.display_name || user.email}.
                      </p>
                      <ApiKeysPanel
                        userId={user._id}
                        emptyHint={`${user.display_name || user.email} has no API keys.`}
                      />
                    </td>
                  </tr>
                )}
                </React.Fragment>
              ))}
            </tbody>
          </table>

          {users.length === 0 && (
            <div className="text-center py-12">
              <UsersIcon className="h-12 w-12 mx-auto mb-4 text-gray-400" />
              <p className="text-gray-500 dark:text-gray-400">No users found</p>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
