// Configuration
const API_URL = 'http://localhost:5000/api';
let currentFileId = null;

// DOM Elements
const themeToggle = document.getElementById('themeToggle');
const uploadArea = document.getElementById('uploadArea');
const fileInput = document.getElementById('fileInput');
const tabs = document.querySelectorAll('.tab');
const sections = document.querySelectorAll('.content-section');

const previewPlaceholder = document.getElementById('previewPlaceholder');
const previewImageContainer = document.getElementById('previewImageContainer');
const previewImage = document.getElementById('previewImage');
const imageName = document.getElementById('imageName');
const imageSize = document.getElementById('imageSize');
const imageStatus = document.getElementById('imageStatus');

let uploadedFile = null;

// Theme Toggle
themeToggle.addEventListener('click', () => {
    document.body.classList.toggle('dark-mode');
    const icon = themeToggle.querySelector('i');
    if (document.body.classList.contains('dark-mode')) {
        icon.classList.remove('fa-moon');
        icon.classList.add('fa-sun');
    } else {
        icon.classList.remove('fa-sun');
        icon.classList.add('fa-moon');
    }
});

// Tab Navigation
tabs.forEach(tab => {
    tab.addEventListener('click', () => {
        const targetTab = tab.dataset.tab;
        tabs.forEach(t => t.classList.remove('active'));
        sections.forEach(s => s.classList.remove('active'));
        tab.classList.add('active');
        document.getElementById(`${targetTab}-section`).classList.add('active');
    });
});

// File Upload - Click to browse
uploadArea.addEventListener('click', () => {
    fileInput.click();
});

// File Upload - File selected
fileInput.addEventListener('change', (e) => {
    const file = e.target.files[0];
    if (file) {
        handleFile(file);
        fileInput.value = ''; 
    }
});

// File Upload - Drag and Drop
uploadArea.addEventListener('dragover', (e) => {
    e.preventDefault();
    uploadArea.classList.add('dragover');
});

uploadArea.addEventListener('dragleave', () => {
    uploadArea.classList.remove('dragover');
});

uploadArea.addEventListener('drop', (e) => {
    e.preventDefault();
    uploadArea.classList.remove('dragover');
    const file = e.dataTransfer.files[0];
    if (file) {
        handleFile(file);
    }
});

// Handle File Upload
async function handleFile(file) {
    const validTypes = ['.tif', '.tiff', '.img', '.hdf', '.jpg', '.jpeg', '.png'];
    const fileExt = '.' + file.name.split('.').pop().toLowerCase();
    
    if (!validTypes.includes(fileExt)) {
        alert('Please upload a valid image file (TIFF, IMG, HDF, JPG, or PNG)');
        return;
    }

    uploadedFile = file;
    uploadArea.innerHTML = `
        <div class="upload-icon loading"><i class="fas fa-spinner fa-spin"></i></div>
        <h2>Uploading ${file.name}...</h2>
        <p class="upload-hint" id="uploadProgressText">Please wait...</p>
    `;

    try {
        const data = await uploadFileWithProgress(file);
        currentFileId = data.file_id;

        const imageUrl = data.converted_url || data.original_url;
        let fullUrl;
        if (imageUrl.startsWith('/api/api/')) {
            fullUrl = `http://localhost:5000${imageUrl.replace('/api/api/', '/api/')}`;
        } else if (imageUrl.startsWith('/api')) {
            fullUrl = `http://localhost:5000${imageUrl}`;
        } else {
            fullUrl = `${API_URL}${imageUrl}`;
        }

        showSuccessAndPreview(fullUrl, file, data);

    } catch (error) {
        console.error('Upload error:', error);
        showPlaceholderPreview(file, `Upload failed: ${error.message}`);
        showUploadSuccess(file);
        switchToTab('preview');
    }
}

// Uploads via XHR (instead of fetch) so large multi-band .tif files show
// real progress instead of the UI appearing to hang for large transfers.
function uploadFileWithProgress(file) {
    return new Promise((resolve, reject) => {
        const formData = new FormData();
        formData.append('file', file);

        const xhr = new XMLHttpRequest();
        xhr.open('POST', `${API_URL}/upload`);

        xhr.upload.addEventListener('progress', (e) => {
            if (e.lengthComputable) {
                const pct = Math.round((e.loaded / e.total) * 100);
                const progressText = document.getElementById('uploadProgressText');
                if (progressText) {
                    progressText.textContent = `${pct}% — ${(e.loaded / 1024 / 1024).toFixed(1)} / ${(e.total / 1024 / 1024).toFixed(1)} MB`;
                }
            }
        });

        xhr.onload = () => {
            let data;
            try {
                data = JSON.parse(xhr.responseText);
            } catch (err) {
                reject(new Error('Unexpected server response'));
                return;
            }
            if (xhr.status >= 200 && xhr.status < 300) {
                resolve(data);
            } else {
                reject(new Error(data.error || `Upload failed (status ${xhr.status})`));
            }
        };

        xhr.onerror = () => reject(new Error('Network error during upload'));
        xhr.send(formData);
    });
}

function showSuccessAndPreview(imageSrc, file, data) {
    updatePreview(imageSrc, file, data);
    showUploadSuccess(file);
    switchToTab('preview');
}

function updatePreview(imageSrc, file, data) {
    previewPlaceholder.style.display = 'none';
    previewImageContainer.style.display = 'block';
    previewImage.src = imageSrc;
    previewImage.style.display = 'block';
    previewImage.alt = file.name;

    imageName.textContent = file.name;
    imageSize.textContent = `File Size: ${(file.size / 1024 / 1024).toFixed(2)} MB`;

    if (data.needs_conversion) {
        imageStatus.innerHTML = '<i class="fas fa-check-circle" style="color: #10b981;"></i> Converted to RGB for visualization';
    } else {
        imageStatus.textContent = '';
    }
}

function showPlaceholderPreview(file, message) {
    previewPlaceholder.style.display = 'none';
    previewImageContainer.style.display = 'block';
    previewImage.style.display = 'none';
    const isError = message.includes('failed') || message.includes('Error');
    const warningColor = isError ? '#ef4444' : '#f59e0b';
    const icon = isError ? 'fa-exclamation-circle' : 'fa-info-circle';

    imageName.innerHTML = `
        <div style="display: flex; align-items: center; gap: 1rem; justify-content: center;">
            <i class="fas fa-file-image" style="font-size: 2rem; color: var(--primary-color);"></i>
            <div style="text-align: left;">
                <div style="font-size: 1.125rem; margin-bottom: 0.25rem; font-weight: 600;">${file.name}</div>
                <div style="font-size: 0.875rem; color: var(--text-secondary); margin-bottom: 0.5rem;">
                    File Size: ${(file.size / 1024 / 1024).toFixed(2)} MB
                </div>
                <div style="font-size: 0.8rem; color: ${warningColor};">
                    <i class="fas ${icon}"></i> <span>${message}</span>
                </div>
            </div>
        </div>
    `;
    imageSize.textContent = '';
    imageStatus.textContent = '';
}

function showUploadSuccess(file) {
    uploadArea.innerHTML = `
        <div class="upload-icon" style="background: linear-gradient(135deg, #10b981, #059669); color: white;">
            <i class="fas fa-check"></i>
        </div>
        <h2>Upload Successful!</h2>
        <p class="upload-hint">${file.name} - ${(file.size / 1024 / 1024).toFixed(2)} MB</p>
        <button class="upload-new-btn" onclick="resetForNewUpload()" style="margin-top: 1rem; padding: 0.5rem 1rem; background: var(--bg-tertiary); border: 1px solid var(--border-color); border-radius: 6px; cursor: pointer; color: var(--text-primary);">
            <i class="fas fa-upload"></i> Upload Different Image
        </button>
    `;
}

function resetForNewUpload() {
    uploadedFile = null;
    currentFileId = null;
    uploadArea.innerHTML = `
        <div class="upload-icon"><i class="fas fa-cloud-upload-alt"></i></div>
        <h2>Drop your multispectral image here</h2>
        <p class="upload-hint">or click to browse files</p>
    `;
    previewPlaceholder.style.display = 'block';
    previewImageContainer.style.display = 'none';
    previewImage.src = '';
    previewImage.style.display = 'block';
    imageStatus.textContent = '';
}

function switchToTab(tabName) {
    tabs.forEach(t => t.classList.remove('active'));
    sections.forEach(s => s.classList.remove('active'));
    const targetTab = document.querySelector(`[data-tab="${tabName}"]`);
    if (targetTab) {
        targetTab.classList.add('active');
    }
    document.getElementById(`${tabName}-section`).classList.add('active');
}

// DOM Elements for Results Buttons
const ndviBtn = document.getElementById('ndviBtn');
const iapBtn = document.getElementById('iapBtn');
let activeAnalysis = null;

// NDVI Button Click Handler
if (ndviBtn) {
    ndviBtn.addEventListener('click', async () => {
        if (!currentFileId) {
            alert('Please upload an image first.');
            return;
        }
        
        ndviBtn.classList.add('active');
        if(iapBtn) iapBtn.classList.remove('active');
        activeAnalysis = 'ndvi';
        
        const resultsDisplay = document.getElementById('resultsDisplay');
        const resultsImage = document.getElementById('resultsImage');
        const resultsTitle = document.getElementById('resultsTitle');
        const resultsDescription = document.getElementById('resultsDescription');
        const downloadBtn = document.getElementById('downloadBtn');
        const ndviLegend = document.getElementById('ndviLegend');
        const iapLegend = document.getElementById('iapLegend');
        
        resultsTitle.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Calculating NDVI...';
        resultsDescription.textContent = 'Processing multispectral image...';
        
        if (resultsDisplay) {
            document.getElementById('resultsPlaceholder').style.display = 'none';
            resultsDisplay.style.display = 'flex';
        }
        
        if (ndviLegend) ndviLegend.style.display = 'flex';
        if (iapLegend) iapLegend.style.display = 'none';

        try {
            const response = await fetch(`${API_URL}/ndvi`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ file_id: currentFileId })
            });
            
            const data = await response.json();
            
            if (data.success) {
                const previewUrl = `http://localhost:5000${data.preview_url}`;
                resultsImage.src = previewUrl;
                
                const downloadUrl = `http://localhost:5000${data.download_url}`;
                downloadBtn.href = downloadUrl;
                
                resultsTitle.textContent = 'NDVI Analysis Result';
                resultsDescription.innerHTML = `
                    <strong>Vegetation Index Visualization</strong><br>
                    <span style="font-size: 0.9rem;">
                        Blue areas indicate water bodies. Yellow/Orange indicates bare soil or sparse vegetation. 
                        Red/Maroon represents dense, healthy vegetation.
                    </span>
                `;
                imageStatus.innerHTML = '<i class="fas fa-check-circle" style="color: #10b981;"></i> NDVI analysis complete';
            } else {
                resultsTitle.innerHTML = '<span style="color: red;">Error</span>';
                resultsDescription.textContent = data.error || 'Failed to calculate NDVI';
            }
        } catch (error) {
            console.error('NDVI Error:', error);
            resultsTitle.innerHTML = '<span style="color: red;">Error</span>';
            resultsDescription.textContent = 'Failed to calculate NDVI. Please try again.';
        }
    });
}

// IAP Button Click Handler
if (iapBtn) {
    iapBtn.addEventListener('click', async () => {
        if (!currentFileId) {
            alert('Please upload an image first.');
            return;
        }
        
        iapBtn.classList.add('active');
        if(ndviBtn) ndviBtn.classList.remove('active');
        activeAnalysis = 'iap';
        
        const resultsDisplay = document.getElementById('resultsDisplay');
        const resultsImage = document.getElementById('resultsImage');
        const resultsTitle = document.getElementById('resultsTitle');
        const resultsDescription = document.getElementById('resultsDescription');
        const downloadBtn = document.getElementById('downloadBtn');
        const ndviLegend = document.getElementById('ndviLegend');
        const iapLegend = document.getElementById('iapLegend');
        
        resultsTitle.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Running Random Forest...';
        resultsDescription.textContent = 'Analyzing texture and spectral bands...';
        
        if (resultsDisplay) {
            document.getElementById('resultsPlaceholder').style.display = 'none';
            resultsDisplay.style.display = 'flex';
        }
        
        if (ndviLegend) ndviLegend.style.display = 'none';
        if (iapLegend) iapLegend.style.display = 'flex';

        try {
            const response = await fetch(`${API_URL}/iap`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ file_id: currentFileId })
            });
            
            const data = await response.json();
            
            if (data.success) {
                const previewUrl = `http://localhost:5000${data.preview_url}`;
                resultsImage.src = previewUrl;
                
                const downloadUrl = `http://localhost:5000${data.download_url}`;
                downloadBtn.href = downloadUrl;
                
                resultsTitle.textContent = 'IAP Classification Result';
                resultsDescription.innerHTML = `
                    <strong>Random Forest Land Cover Map</strong><br>
                    <span style="font-size: 0.9rem;">
                        Uses spectral bands + texture analysis to distinguish invasive species,
                        including Katbos and Bankrot Bos. Vivid red areas indicate Invasive,
                        lighter red/pink indicates Katbos, burnt orange indicates Bankrot Bos,
                        and tan indicates bare soil (e.g. dirt roads).
                    </span>
                    ${data.training_sample_counts ? `
                        <br><br>
                        <span style="font-size: 0.8rem; color: var(--text-secondary);">
                            <strong>Training pixels used (this image):</strong><br>
                            ${Object.entries(data.training_sample_counts)
                                .map(([cls, n]) => {
                                    const borrowedFile = data.training_sample_borrowed_from?.[cls];
                                    if (n === 0) return `${cls}: 0 ⚠️ no coverage anywhere — not predictable`;
                                    if (borrowedFile) return `${cls}: ${n.toLocaleString()} (includes samples borrowed from ${borrowedFile})`;
                                    return `${cls}: ${n.toLocaleString()}`;
                                })
                                .join('<br>')}
                        </span>
                    ` : ''}
                `;
            } else {
                resultsTitle.innerHTML = '<span style="color: red;">Error</span>';
                resultsDescription.textContent = data.error || 'Failed to classify';
            }
        } catch (error) {
            console.error('IAP Error:', error);
            resultsTitle.innerHTML = '<span style="color: red;">Error</span>';
            resultsDescription.textContent = 'Failed to run classification.';
        }
    });
}

// Clear Results Function
function clearResults() {
    const resultsDisplay = document.getElementById('resultsDisplay');
    const resultsPlaceholder = document.getElementById('resultsPlaceholder');
    const resultsImage = document.getElementById('resultsImage');
    const ndviLegend = document.getElementById('ndviLegend');
    const iapLegend = document.getElementById('iapLegend');
    
    if (resultsDisplay) {
        resultsDisplay.style.display = 'none';
        resultsDisplay.classList.remove('active');
    }
    if (resultsPlaceholder) resultsPlaceholder.style.display = 'block';
    if (resultsImage) resultsImage.src = '';
    if (ndviLegend) ndviLegend.style.display = 'none';
    if (iapLegend) iapLegend.style.display = 'none';
    
    if (ndviBtn) ndviBtn.classList.remove('active');
    if (iapBtn) iapBtn.classList.remove('active');
    activeAnalysis = null;
    
    console.log('Results cleared');
}

window.switchToTab = switchToTab;
window.resetForNewUpload = resetForNewUpload;
window.clearResults = clearResults;