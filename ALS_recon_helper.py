import sys
import os
import subprocess
import time
import numpy as np
import matplotlib.pyplot as plt
import ipywidgets as widgets
import scipy.signal as signal
from scipy.fft import fft, ifft, fftfreq, fftshift
from skimage import transform, io
import tomopy
import astra
import svmbir
import dxchange

"""
Functions are in rough order of when they are called in notebook
Need to add comments
"""

def check_for_gpu():
    try:
        subprocess.check_output('nvidia-smi')
        print('Nvidia GPU detected, will use to reconstruct!')
        return True
    except Exception: # if command not found, cant talk to GPU (or doesnt exists)
        print('No Nvidia GPU in system, will use CPU')
        return False

def get_directory_filelist(path,max_num=10000):
    filenamelist = os.listdir(path)
    filenamelist.sort()
    for i in range(len(filenamelist)-1,np.maximum(len(filenamelist)-max_num,-1),-1):
        print(f'{i}: {filenamelist[i]}')
    return filenamelist

def read_metadata(path,print_flag=True):
    numslices = int(dxchange.read_hdf5(path, "/measurement/instrument/detector/dimension_y")[0])
    numrays = int(dxchange.read_hdf5(path, "/measurement/instrument/detector/dimension_x")[0])
    pxsize = dxchange.read_hdf5(path, "/measurement/instrument/detector/pixel_size")[0] / 10.0  # /10 to convert units from mm to cm
    numangles = int(dxchange.read_hdf5(path, "/process/acquisition/rotation/num_angles")[0])
    propagation_dist = dxchange.read_hdf5(path, "/measurement/instrument/camera_motor_stack/setup/camera_distance")[1]
    kev = dxchange.read_hdf5(path, "/measurement/instrument/monochromator/energy")[0] / 1000
    angularrange = dxchange.read_hdf5(path, "/process/acquisition/rotation/range")[0]
    filename = os.path.split(path)[-1]
    if print_flag:
        print(f'{filename}:')
        print(f'numslices: {numslices}, rays: {numrays}, numangles: {numangles}')
        print(f'angularrange: {angularrange}, \pxsize: {pxsize*10000} um, distance: {propagation_dist} mm. energy: {kev} keV')
        if kev>100:
            print('white light mode detected; energy is set to 30 kev for the phase retrieval function')
        
    return {'numslices': numslices,
            'numrays': numrays,
            'pxsize': pxsize,
            'numangles': numangles,
            'propagation_dist': propagation_dist,
            'kev': kev,
            'angularrange': angularrange}

def read_data(path, proj=None, sino=None, downsample_factor=None, prelog=False, args=None):
    tomo, flat, dark, angles = dxchange.exchange.read_aps_tomoscan_hdf5(path, proj=proj, sino=sino, dtype=np.float32)
    angles = angles[proj].squeeze()
    tomopy.normalize(tomo, flat, dark, out=tomo)
        # flat = np.asarray([transform.downscale_local_mean(f, (downsample_factor,downsample_factor), cval=0).astype(f.dtype) for f in flat])
        # dark = np.asarray([transform.downscale_local_mean(d, (downsample_factor,downsample_factor), cval=0).astype(d.dtype) for d in dark])
    if prelog:
        # downsampling pre-log can lead to bright halo in recon with radius = nrays -- may need to mask recon
        if downsample_factor and downsample_factor!=1:
            tomo = np.asarray([transform.downscale_local_mean(proj, (downsample_factor,downsample_factor), cval=0).astype(proj.dtype) for proj in tomo])
        return tomo, angles
    if args:
        tomo = preprocess_tomo(tomo, args)
    tomopy.minus_log(tomo, out=tomo)
    # downsampling post-log is better
    if downsample_factor and downsample_factor!=1:
        tomo = np.asarray([transform.downscale_local_mean(proj, (downsample_factor,downsample_factor), cval=0).astype(proj.dtype) for proj in tomo])
    return tomo, angles

# normalize with flat/dark, threshold transmission, and take negative log
def preprocess_tomo(tomo, args):
    # sarepy remove stripes
    tomo = tomopy.remove_all_stripe(tomo,snr=args['snr'], la_size=args['la_size'], sm_size=args['sm_size'])
    # threshold low measurements
    if args['minimum_transmission']:
        tomo[tomo < args['minimum_transmission'] ] = args['minimum_transmission']

    return tomo

# this is the default processing done in Dula's reconstruction.py
def preprocess_tomo_orig(tomo, flat, dark,
                    outlier_diff1D=750, # difference between good data and outlier data (outlier removal) 
                    outlier_size1D=3, # for remove_outlier1d, hardcoded from reconstruction.py
                    minimum_transmission = 0.01, # from reconstruction.py
                    ringSigma=3,  # for remove stripe fw, from reconstruction.py
                    ringLevel=8,  # for remove stripe fw, from reconstruction.py
                    ringWavelet='db5', # for remove stripe fw, from reconstruction.py
                    **kwargs):

    # median filter -- WHY ACROSS ROTATION AXIS?
    tomopy.misc.corr.remove_outlier1d(tomo, outlier_diff1D, size=outlier_size1D, axis=0, ncore=None, out=tomo)
    # normalize with flat/dark, threshold transmission, and take negative log
    if minimum_transmission:
        tomo[tomo < minimum_transmission] = minimum_transmission
    tomopy.minus_log(tomo, out=tomo)
    # post-log, wavelet-based ring removal
    tomo = tomopy.remove_stripe_fw(tomo, sigma=ringSigma, level=ringLevel, pad=True, wname=ringWavelet)
    return tomo

def mask_recon(recon,r=None):
    # Need to add this to remove bright halo
    x, y = np.arange(recon.shape[1]), np.arange(recon.shape[2])
    X,Y = np.meshgrid(x-x.mean(),y-y.mean(),indexing='ij')
    if r is None:
        r = np.maximum(recon.shape[1],recon.shape[2])
    recon[:,(X**2 + Y**2 > (r)**2)] = 0
    return recon

def auto_find_cor(path):
    metadata = read_metadata(path,print_flag=False)
    tomo, _ = read_data(path, proj=slice(0,None,metadata['numangles']-1),downsample_factor=None)
    first_proj, last_proj_flipped = tomo[0], tomo[1,:,::-1]
    cor = tomopy.find_center_pc(tomo[0], tomo[-1], tol=0.25)
    cor = cor - tomo.shape[2]/2
    return cor

def shift_projections(projs, COR, yshift=0):
    translateFunction = transform.SimilarityTransform(translation=(COR, yshift))
    if projs.ndim == 2:
        shifted = transform.warp(projs, translateFunction)
    elif projs.ndim == 3:
        # Apply translation with interpolation to projection[n]
        shifted = np.asarray([transform.warp(proj, translateFunction) for proj in projs])
    else:
        print('projs must be 2D or 3D')
        return
    return shifted

def astra_fbp_recon(tomo,angles,center=0,fc=None,gpu=False):
    
    if fc:
        N = np.minimum(100,tomo.shape[2])
        lpf = signal.firwin(N,fc) # time domain filter taps
        _, LPF = np.abs(signal.freqz(lpf,a=1,worN=tomo.shape[2],whole=True)) # freq domain filter
        tomo = np.real(ifft( fft(tomo, axis=2) * LPF, axis=2)) # apply filter in freq domain
        # tomo = signal.lfilter(b,1,axis=2)
        # tomo = signal.filtfilt(b,1,tomo,axis=2)
    
    if gpu:
        rec = tomopy.recon(tomo, angles,
                           center=center + tomo.shape[2]/2,
                           algorithm=tomopy.astra,
                           options={'method':"FBP_CUDA", 'proj_type':'cuda'})
    else:
        rec = tomopy.recon(tomo, angles,
                           center=center + tomo.shape[2]/2,
                           algorithm=tomopy.astra,
                           options={'method':"FBP", 'proj_type':'linear'})
    return rec

def astra_cgls_recon(tomo,angles,center=0,num_iter=20,gpu=False):
    
    if gpu:
        rec = tomopy.recon(tomo, angles,
                           center=center + tomo.shape[2]/2,
                           algorithm=tomopy.astra,
                           options={'method':"CGLS_CUDA", 'proj_type':'cuda', 'num_iter': num_iter})
    else:
        rec = tomopy.recon(tomo, angles,
                           center=center + tomo.shape[2]/2,
                           algorithm=tomopy.astra,
                           options={'method':"CGLS", 'proj_type':'linear', 'num_iter': num_iter})
    return rec

def astra_fbp_recon_3d(tomo,angles_or_vectors,vectors=False,center=0,fc=None):
    numslices = tomo.shape[1]
    numrays = tomo.shape[2]
    if vectors: # vectors created with astra_projection_wrapper
        vectors = angles_or_vectors
        proj_geom = astra.create_proj_geom('parallel3d_vec', numslices, numrays, vectors)
    else: # just angles, not vectors
        angles = -angles_or_vectors # need to be negative to match tomopy
        proj_geom = astra.create_proj_geom('parallel3d', 1.0, 1.0, numslices, numrays, angles)
        proj_geom = astra.geom_postalignment(proj_geom, [-center])
    
    # filtered
    ramp_filter_freq_domain = transform.radon_transform._get_fourier_filter(tomo.shape[2],'None').squeeze()
    if fc:
        N = np.minimum(100,tomo.shape[2])
        lpf = signal.firwin(N,fc) # time domain filter taps
        _, LPF = np.abs(signal.freqz(lpf,a=1,worN=tomo.shape[2],whole=True)) # zero-phase freq domain filter
        ramp_filter_freq_domain *= LPF
    tomo = np.real(ifft( fft(tomo, axis=2) * ramp_filter_freq_domain, axis=2))

    # backprojection
    cfg = astra.astra_dict('BP3D_CUDA')    
    vol_geom = astra.create_vol_geom(numrays,numrays,numslices)
    rec_id = astra.data3d.create('-vol', vol_geom)
    proj_id = astra.data3d.create('-proj3d', proj_geom, tomo.transpose(1,0,2))
    cfg['ReconstructionDataId'] = rec_id
    cfg['ProjectionDataId'] = proj_id    
    alg_id = astra.algorithm.create(cfg)
    astra.algorithm.run(alg_id)
    rec = astra.data3d.get(rec_id)
    return rec

def astra_cgls_recon_3d(tomo,angles_or_vectors,vectors=False,center=0,num_iter=20):
    numslices = tomo.shape[1]
    numrays = tomo.shape[2]
    if vectors: # vectors created with astra_projection_wrapper
        vectors = angles_or_vectors
        proj_geom = astra.create_proj_geom('parallel3d_vec', numslices, numrays, vectors)
    else: # just angles, not vectors
        angles = -angles_or_vectors # need to be negative to match tomopy
        proj_geom = astra.create_proj_geom('parallel3d', 1.0, 1.0, numslices, numrays, angles)
        proj_geom = astra.geom_postalignment(proj_geom, [-center])
    
    cfg = astra.astra_dict('CGLS3D_CUDA')        
    vol_geom = astra.create_vol_geom(numrays,numrays,numslices)
    rec_id = astra.data3d.create('-vol', vol_geom)
    proj_id = astra.data3d.create('-proj3d', proj_geom, tomo.transpose(1,0,2))
    cfg['ReconstructionDataId'] = rec_id
    cfg['ProjectionDataId'] = proj_id    
    alg_id = astra.algorithm.create(cfg)
    astra.algorithm.run(alg_id,num_iter)
    rec = astra.data3d.get(rec_id)
    return rec

def cache_svmbir_projector(img_size,num_angles,num_threads=None):

    for i,(sz,nang) in enumerate(zip(img_size,num_angles)):
        print(f"Starting size={sz[0]}x{sz[1]}")
        img = svmbir.phantom.gen_shepp_logan(sz[0],sz[1])[np.newaxis]
        angles = np.linspace(0,np.pi,nang)
        t0 = time.time()
        tomo = svmbir.project(img, angles, img.shape[2],
                              num_threads=num_threads,
                              verbose=0,
                              svmbir_lib_path=get_svmbir_cache_dir())
        t = time.time() - t0
        print(f"Finisehd: time={t}")    
        
def svmbir_fbp(tomo,angles,cor=0,num_threads=None):   
    fourier_filter = transform.radon_transform._get_fourier_filter(tomo.shape[2],'ramp').squeeze()
    filtered_tomo = np.real(ifft( fft(tomo, axis=2) * fourier_filter, axis=2))
    rec = svmbir.backproject(filtered_tomo, angles,
                             geometry='parallel',
                             center_offset=cor,
                             num_threads=num_threads,
                             svmbir_lib_path=get_svmbir_cache_dir(),
                             verbose=False)
    return rec
   
def get_svmbir_cache_dir():
    return '//global/cfs/cdirs/als/users/tomography_notebooks/svmbir_cache'
    # s = os.popen("echo $NERSC_HOST")
    # out = s.read()
    # if 'cori' in out:
    #     return '/global/cscratch1/sd/dperl/svmbir_cache'
    # elif 'perlmutter' in out:
    #     return '/pscratch/sd/d/dperl/svmbir_cache'
    # else:
    #     sys.exit('not or cori or perlmutter -- throwing error')
    

def plot_recon(recon,fignum=1,figsize=4,clims=None):
    if clims is None:
        clims = [np.percentile(recon,1),np.percentile(recon,99)]
    if plt.fignum_exists(fignum): plt.close(fignum)
    fig = plt.figure(num=fignum,figsize=(figsize, figsize))
    axs = plt.gca()
    img = axs.imshow(recon[0],cmap='gray',vmin=clims[0],vmax=clims[1])    
    clim_slider = widgets.interactive(set_clim, img=widgets.fixed(img),
                                      clims=widgets.FloatRangeSlider(description='Color Scale', layout=widgets.Layout(width='50%'),
                                                                           min=recon.min(), max=recon.max(),
                                                                           step=(recon.max()-recon.min())/500, value=img.get_clim()))

    return img, axs, clim_slider

def set_cor(i,img,axs,recons,cors):
    img.set_data(recons[i][0])
    axs.set_title(f"COR = {cors[i]} pixels (at full res)")
    
def set_slice(slice_num,img,axs,recon,slices):
    if not isinstance(axs,list): axs = [axs] 
    if not isinstance(img,list): img = [img]
    for ax,im in zip(axs,img):
        im.set_data(recon[slice_num])
        ax.set_title(f"Slice {slices[slice_num]}")

def set_clim(img,clims):
    img.set_clim(vmin=clims[0],vmax=clims[1])        
    
def plot_0_and_180_proj_diff(first_proj,last_proj_flipped,init_cor=0,fignum=1,figsize=4):
    if plt.fignum_exists(num=fignum): plt.close(fignum)
    fig, axs = plt.subplots(figsize=(figsize,figsize), constrained_layout=True, num=fignum)
    fig.canvas.toolbar_position = 'right'
    fig.canvas.header_visible = False
    shifted_last_proj = shift_projections(last_proj_flipped, init_cor, yshift=0)
    img = axs.imshow(first_proj - shifted_last_proj, cmap='gray',vmin=-.1,vmax=.1)
    axs.set_title(f"COR: 0, y_shift: 0")

    slider_dx = widgets.FloatSlider(description='Shift X', min=-800, max=800, step=0.5, value=init_cor, layout=widgets.Layout(width='50%'))
    slider_dy = widgets.FloatSlider(description='Shift Y', min=-800, max=800, step=0.5, value=0, layout=widgets.Layout(width='50%'))
    ui = widgets.VBox([slider_dx, slider_dy])
    sliders = widgets.interactive_output(shift_proj,{'dx':slider_dx,'dy':slider_dy,
                                                     'img':widgets.fixed(img),'axs':widgets.fixed(axs),
                                                     'first_proj':widgets.fixed(first_proj),'last_proj_flipped':widgets.fixed(last_proj_flipped)})
    
    return axs, img, ui, sliders

def shift_proj(dx,dy,img,axs,first_proj,last_proj_flipped,downsample_factor=1):
    shifted_last_proj = shift_projections(last_proj_flipped, dx, yshift=dy)
    img.set_data(first_proj - shifted_last_proj)
    axs.set_title(f"COR: {-downsample_factor*dx/2}, y_shift: {downsample_factor*dy/2}")
    
def plot_recon_comparison(recon1,recon2,titles=['',''],fignum=1,figsize=4):
    if plt.fignum_exists(fignum): plt.close(fignum)
    fig, axs = plt.subplots(1,2,num=fignum,figsize=(2*figsize,figsize),sharex=True,sharey=True)
    img = [None, None]
    img[0] = axs[0].imshow(recon1[0],cmap='gray',vmin=np.percentile(recon1[0],1),vmax=np.percentile(recon1[0],99))
    axs[0].set_title(titles[0])
    img[1] = axs[1].imshow(recon2[0],cmap='gray',vmin=np.percentile(recon2[0],1),vmax=np.percentile(recon2[0],99))
    axs[1].set_title(titles[1])
    plt.tight_layout()
    return axs, img
