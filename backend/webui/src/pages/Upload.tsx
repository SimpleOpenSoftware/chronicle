import React, { useCallback, useRef, useState } from 'react'
import {
  AlertCircle,
  ArrowRight,
  Brain,
  CheckCircle,
  File,
  PenLine,
  RefreshCw,
  Upload as UploadIcon,
  X,
} from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import { dataAuditApi, uploadApi } from '../services/api'
import { useAuth } from '../contexts/AuthContext'
import { Button, IconButton, Input, Alert } from '../components/ui'

const SUPPORTED_EXTENSIONS = ['.wav', '.mp3', '.m4a', '.flac', '.ogg', '.mp4', '.webm']
const VIDEO_EXTENSIONS = ['.mp4', '.webm']
type UploadMode = 'memory' | 'annotation'

interface UploadFile {
  file: File
  id: string
  status: 'pending' | 'uploading' | 'success' | 'error'
  error?: string
}

export default function Upload() {
  const navigate = useNavigate()
  const [files, setFiles] = useState<UploadFile[]>([])
  const [isUploading, setIsUploading] = useState(false)
  const [dragActive, setDragActive] = useState(false)
  const [uploadProgress, setUploadProgress] = useState(0)
  const [gdriveFolderId, setGdriveFolderId] = useState('')
  const [videoWarning, setVideoWarning] = useState(false)
  const [uploadMode, setUploadMode] = useState<UploadMode>('memory')
  const [uploadSummary, setUploadSummary] = useState('')
  const [importedDataset, setImportedDataset] = useState<{
    id: string
    clipCount: number
  } | null>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)

  const annotationOnly = uploadMode === 'annotation'

  const { isAdmin } = useAuth()

  const generateId = () => Math.random().toString(36).substr(2, 9)

  const [gdriveUploadStatus, setGdriveUploadStatus] = useState<{
    type: 'success' | 'error' | null
    message: string
  }>({
    type: null,
    message: ''
  })

  // Handle Google Drive folder submission
  const handleGDriveSubmit = async () => {
    if (!gdriveFolderId) return

    setIsUploading(true)
    setGdriveUploadStatus({ type: null, message: '' })

    try {
      await uploadApi.uploadFromGDriveFolder({
        gdrive_folder_id: gdriveFolderId,
        device_name: 'upload',
        annotation_only: annotationOnly,
      })

      setGdriveUploadStatus({
        type: 'success',
        message: 'Google Drive folder submitted successfully.',
      })

      setGdriveFolderId('')
    } catch (err: any) {
      setGdriveUploadStatus({
        type: 'error',
        message: err?.response?.data?.detail || 'Failed to upload folder.',
      })
    } finally {
      setIsUploading(false)
    }
  }

  const handleFileSelect = (selectedFiles: FileList | null) => {
    if (!selectedFiles) return

    const acceptedFiles = Array.from(selectedFiles).filter((file) => {
      const ext = '.' + file.name.split('.').pop()?.toLowerCase()
      if (annotationOnly && ext === '.zip') return true
      return (
        file.type.startsWith('audio/') ||
        file.type.startsWith('video/') ||
        SUPPORTED_EXTENSIONS.includes(ext)
      )
    })

    const datasetFiles = acceptedFiles.filter((file) => file.name.toLowerCase().endsWith('.zip'))
    if (datasetFiles.length > 0) {
      const datasetFile = datasetFiles[0]
      setFiles([{
        file: datasetFile,
        id: generateId(),
        status: 'pending',
      }])
      setVideoWarning(false)
      setUploadSummary('')
      setImportedDataset(null)
      return
    }

    const hasVideo = acceptedFiles.some((file) => {
      const ext = '.' + file.name.split('.').pop()?.toLowerCase()
      return file.type.startsWith('video/') || VIDEO_EXTENSIONS.includes(ext)
    })
    if (hasVideo) setVideoWarning(true)

    const newFiles: UploadFile[] = acceptedFiles.map((file) => ({
      file,
      id: generateId(),
      status: 'pending',
    }))

    setFiles((prev) => [...prev, ...newFiles])
    setUploadSummary('')
    setImportedDataset(null)
  }

  const removeFile = (id: string) => {
    setFiles(files.filter((f) => f.id !== id))
  }

  const handleDrag = useCallback((e: React.DragEvent) => {
    e.preventDefault()
    e.stopPropagation()
    if (e.type === 'dragenter' || e.type === 'dragover') setDragActive(true)
    else if (e.type === 'dragleave') setDragActive(false)
  }, [])

  const handleDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault()
    e.stopPropagation()
    setDragActive(false)
    handleFileSelect(e.dataTransfer.files)
  }, [annotationOnly])

  const selectMode = (mode: UploadMode) => {
    if (mode === uploadMode) return
    setUploadMode(mode)
    setFiles([])
    setVideoWarning(false)
    setUploadProgress(0)
    setUploadSummary('')
    setImportedDataset(null)
    if (fileInputRef.current) fileInputRef.current.value = ''
  }

  const uploadFiles = async () => {
    const pendingFiles = files.filter((file) => file.status === 'pending')
    if (pendingFiles.length === 0) return

    setIsUploading(true)
    setUploadProgress(0)

    try {
      const pendingIds = new Set(pendingFiles.map((file) => file.id))
      const isDatasetImport =
        annotationOnly &&
        pendingFiles.length === 1 &&
        pendingFiles[0].file.name.toLowerCase().endsWith('.zip')
      const formData = new FormData()
      if (isDatasetImport) {
        formData.append('dataset', pendingFiles[0].file)
      } else {
        pendingFiles.forEach(({ file }) => formData.append('files', file))
      }

      setFiles((prev) =>
        prev.map((f) => pendingIds.has(f.id) ? { ...f, status: 'uploading' } : f)
      )

      const response = isDatasetImport
        ? await dataAuditApi.importDataset(formData, setUploadProgress)
        : await uploadApi.uploadAudioFiles(formData, setUploadProgress, { annotationOnly })

      setFiles((prev) =>
        prev.map((f) => pendingIds.has(f.id) ? { ...f, status: 'success' } : f)
      )
      setUploadSummary(
        isDatasetImport
          ? `${response.data.summary.imported} clips imported for review${response.data.summary.skipped ? `, ${response.data.summary.skipped} already present` : ''}.`
          : annotationOnly
            ? `${pendingFiles.length} file${pendingFiles.length === 1 ? '' : 's'} sent to the annotation workspace.`
            : `${pendingFiles.length} file${pendingFiles.length === 1 ? '' : 's'} sent for processing.`
      )
      if (isDatasetImport) {
        setImportedDataset({
          id: response.data.dataset_id,
          clipCount: response.data.summary.imported + response.data.summary.skipped,
        })
      }
    } catch (err: any) {
      console.error('Upload failed:', err)

      setFiles((prev) =>
        prev.map((f) =>
          f.status === 'uploading'
            ? {
                ...f,
                status: 'error',
                error: err?.response?.data?.error || err.message || 'Upload failed',
              }
            : f
        )
      )
      setUploadSummary('')
      setImportedDataset(null)
    } finally {
      setIsUploading(false)
      setUploadProgress(100)
    }
  }


  const clearCompleted = () => {
    const remaining = files.filter((f) => f.status === 'pending' || f.status === 'uploading')
    setFiles(remaining)
    if (remaining.length === 0) setVideoWarning(false)
  }

  const formatFileSize = (bytes: number) => {
    if (bytes === 0) return '0 Bytes'
    const k = 1024
    const sizes = ['Bytes', 'KB', 'MB', 'GB']
    const i = Math.floor(Math.log(bytes) / Math.log(k))
    return `${(bytes / Math.pow(k, i)).toFixed(2)} ${sizes[i]}`
  }

  const getStatusIcon = (status: UploadFile['status']) => {
    switch (status) {
      case 'success':
        return <CheckCircle className="h-5 w-5 text-green-500" />
      case 'error':
        return <AlertCircle className="h-5 w-5 text-red-500" />
      case 'uploading':
        return <RefreshCw className="h-5 w-5 text-blue-500 animate-spin" />
      default:
        return <File className="h-5 w-5 text-gray-500" />
    }
  }

  if (!isAdmin) {
    return (
      <div className="text-center">
        <UploadIcon className="h-12 w-12 mx-auto mb-4 text-gray-400" />
        <h2 className="text-xl font-semibold text-gray-900 dark:text-gray-100 mb-2">
          Access Restricted
        </h2>
        <p className="text-gray-600 dark:text-gray-400">
          You need administrator privileges to upload audio files.
        </p>
      </div>
    )
  }

  return (
    <div>
      {/* Header */}
      <div className="flex items-center space-x-2 mb-6">
        <UploadIcon className="h-6 w-6 text-blue-600" />
        <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">
          Upload Audio
        </h1>
      </div>

      <div className="mb-6">
        <div className="inline-grid grid-cols-2 gap-1 rounded-lg bg-gray-100 p-1 dark:bg-gray-800">
          <button
            type="button"
            aria-pressed={uploadMode === 'memory'}
            onClick={() => selectMode('memory')}
            className={`flex min-h-10 items-center justify-center gap-2 rounded-md px-4 py-2 text-sm font-medium transition-colors ${
              uploadMode === 'memory'
                ? 'bg-white text-gray-900 shadow-sm dark:bg-gray-700 dark:text-gray-100'
                : 'text-gray-600 hover:text-gray-900 dark:text-gray-400 dark:hover:text-gray-100'
            }`}
          >
            <Brain className="h-4 w-4" />
            Personal recordings
          </button>
          <button
            type="button"
            aria-pressed={uploadMode === 'annotation'}
            onClick={() => selectMode('annotation')}
            className={`flex min-h-10 items-center justify-center gap-2 rounded-md px-4 py-2 text-sm font-medium transition-colors ${
              uploadMode === 'annotation'
                ? 'bg-white text-gray-900 shadow-sm dark:bg-gray-700 dark:text-gray-100'
                : 'text-gray-600 hover:text-gray-900 dark:text-gray-400 dark:hover:text-gray-100'
            }`}
          >
            <PenLine className="h-4 w-4" />
            Annotation data
          </button>
        </div>
        <p className="mt-2 text-sm text-gray-600 dark:text-gray-400">
          {annotationOnly
            ? 'Excluded from memory.'
            : 'Destination: Main'}
        </p>
      </div>

      {/* Google Drive Folder Upload */}
      <details className="mb-6 rounded-lg border border-[var(--tape-line)] p-3">
        <summary className="cursor-pointer text-sm text-[var(--tape-focus)]">Import from Google Drive</summary>
        <label htmlFor="gdrive-folder-id" className="mt-3 block mb-2 font-medium text-gray-900 dark:text-gray-100">
          Google Drive folder ID
        </label>

        <div className="flex flex-col gap-2 sm:flex-row">
          <Input
            id="gdrive-folder-id"
            type="text"
            value={gdriveFolderId}
            onChange={(e) => setGdriveFolderId(e.target.value)}
            placeholder="1AbCdEfGhIjKlMnOpQrStUvWxYz123456"
            className="min-w-0 flex-1"
          />

          <Button
            variant="primary"
            size="md"
            onClick={handleGDriveSubmit}
            disabled={isUploading || !gdriveFolderId}
            className="w-full whitespace-nowrap sm:w-auto"
          >
            {isUploading ? 'Submitting...' : annotationOnly ? 'Import for review' : 'Process folder'}
          </Button>
        </div>

        {gdriveUploadStatus.type && (
          <Alert tone={gdriveUploadStatus.type === 'success' ? 'success' : 'danger'} className="mt-3">
            {gdriveUploadStatus.message}
          </Alert>
        )}
      </details>

      {/* Drop Zone */}
      <div
        className={`relative border-2 border-dashed rounded-lg p-8 text-center transition-colors ${
          dragActive
            ? 'border-blue-400 bg-blue-50 dark:bg-blue-900/10'
            : 'border-gray-300 dark:border-gray-600 hover:border-blue-400'
        }`}
        onDragEnter={handleDrag}
        onDragLeave={handleDrag}
        onDragOver={handleDrag}
        onDrop={handleDrop}
      >
        <UploadIcon className="h-12 w-12 mx-auto mb-4 text-gray-400" />
        <h3 className="text-lg font-medium text-gray-900 dark:text-gray-100 mb-2">
          {annotationOnly ? 'Drop audio or a Chronicle dataset ZIP' : 'Drop audio files here'}
        </h3>
        <p className="text-sm text-gray-600 dark:text-gray-400 mb-4">
          {annotationOnly
            ? 'ZIP datasets keep their existing transcripts; audio files are transcribed.'
            : 'WAV, MP3, M4A, FLAC, OGG, MP4, or WebM'}
        </p>

        <input
          ref={fileInputRef}
          type="file"
          aria-label="Choose audio files or an annotation dataset"
          multiple
          accept={`${annotationOnly ? '.zip,' : ''}audio/*,video/mp4,video/webm,.wav,.mp3,.m4a,.flac,.ogg,.mp4,.webm`}
          onChange={(e) => handleFileSelect(e.target.files)}
          className="absolute inset-0 w-full h-full opacity-0 cursor-pointer"
        />

        <Button type="button" variant="primary" size="md" onClick={() => fileInputRef.current?.click()}>
          Select files
        </Button>
      </div>

      {/* Video Warning */}
      {videoWarning && (
        <Alert tone="warning" className="mt-4" icon={<AlertCircle className="h-5 w-5 flex-shrink-0" />}>
          Video files detected — only the audio track will be extracted.
        </Alert>
      )}

      {/* File List */}
      {files.length > 0 && (
        <div className="mt-8">
          <div className="flex justify-between items-center mb-4">
            <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100">
              Files ({files.length})
            </h2>
            <div className="flex space-x-2">
              <Button variant="secondary" onClick={clearCompleted}>
                Clear Completed
              </Button>
              <Button
                variant="primary"
                size="md"
                onClick={uploadFiles}
                disabled={isUploading || files.every((f) => f.status !== 'pending')}
              >
                {isUploading
                  ? 'Uploading...'
                  : annotationOnly
                    ? files.some((f) => f.file.name.toLowerCase().endsWith('.zip'))
                      ? 'Import dataset'
                      : 'Add to annotation workspace'
                    : 'Process files'}
              </Button>
            </div>
          </div>

          <div className="space-y-2">
            {files.map((uploadFile) => (
              <div
                key={uploadFile.id}
                className="flex items-center justify-between p-4 bg-gray-50 dark:bg-gray-700 rounded-lg border border-gray-200 dark:border-gray-600"
              >
                <div className="flex items-center space-x-3 flex-1">
                  {getStatusIcon(uploadFile.status)}
                  <div className="flex-1 min-w-0">
                    <div className="font-medium text-gray-900 dark:text-gray-100 truncate">
                      {uploadFile.file.name}
                    </div>
                    <div className="text-sm text-gray-600 dark:text-gray-400">
                      {formatFileSize(uploadFile.file.size)}
                      {uploadFile.error && (
                        <span className="text-red-600 dark:text-red-400 ml-2">
                          • {uploadFile.error}
                        </span>
                      )}
                    </div>
                  </div>
                </div>

                <div className="flex items-center space-x-2">
                  <span
                    className={`text-sm font-medium ${
                      uploadFile.status === 'success'
                        ? 'text-green-600'
                        : uploadFile.status === 'error'
                        ? 'text-red-600'
                        : uploadFile.status === 'uploading'
                        ? 'text-blue-600'
                        : 'text-gray-600 dark:text-gray-400'
                    }`}
                  >
                    {uploadFile.status.charAt(0).toUpperCase() + uploadFile.status.slice(1)}
                  </span>

                  {uploadFile.status === 'pending' && (
                    <IconButton label="Remove file" danger onClick={() => removeFile(uploadFile.id)}>
                      <X className="h-4 w-4" />
                    </IconButton>
                  )}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {uploadSummary && (
        <div className="mt-6 flex flex-col gap-3 rounded-lg border border-green-200 bg-green-50 p-4 text-sm text-green-800 dark:border-green-800 dark:bg-green-900/20 dark:text-green-300 sm:flex-row sm:items-center sm:justify-between">
          <div className="flex items-start gap-2">
            <CheckCircle className="mt-0.5 h-4 w-4 flex-shrink-0" />
            <span>{uploadSummary}</span>
          </div>
          {importedDataset && importedDataset.clipCount > 0 && (
            <button
              type="button"
              onClick={() =>
                navigate(`/data-audit?dataset=${encodeURIComponent(importedDataset.id)}`)
              }
              className="flex min-h-10 w-full items-center justify-center gap-2 rounded-md bg-green-700 px-4 py-2 font-medium text-white hover:bg-green-800 sm:w-auto"
            >
              Review {importedDataset.clipCount} clip{importedDataset.clipCount === 1 ? '' : 's'}
              <ArrowRight className="h-4 w-4" />
            </button>
          )}
        </div>
      )}

      {/* Upload Progress */}
      {isUploading && (
        <div className="mt-6 p-4 bg-blue-50 dark:bg-blue-900/20 border border-blue-200 dark:border-blue-800 rounded-lg">
          <div className="flex items-center justify-between mb-2">
            <span className="text-sm font-medium text-blue-700 dark:text-blue-300">
              Submitting files...
            </span>
            <span className="text-sm text-blue-600 dark:text-blue-400">
              {uploadProgress}%
            </span>
          </div>
          <div className="w-full bg-blue-200 dark:bg-blue-800 rounded-full h-2">
            <div
              className="bg-blue-600 h-2 rounded-full transition-all duration-300"
              style={{ width: `${uploadProgress}%` }}
            />
          </div>

        </div>
      )}

    </div>
  )
}
