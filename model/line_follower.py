# Copyright 2020 Adobe
# All Rights Reserved.

# NOTICE: Adobe permits you to use, modify, and distribute this file in
# accordance with the terms of the Adobe license agreement accompanying
# it.
import torch
from base import BaseModel
import torch.nn as nn
from model.gridgen import AffineGridGen, PerspectiveGridGen, GridGen
from model.lf_loss import getMinimumDists
import numpy as np
from utils import transformation_utils
#from lf_cnn import makeCnn
#from fast_patch_view import get_patches

def b_inv(b_mat):
    eye = b_mat.new_ones(b_mat.size(-1)).diag().expand_as(b_mat)
    b_inv, _ = torch.gesv(eye, b_mat)
    return b_inv

def get_xyrs(mats):
    x=mats[:,2,0]
    y=mats[:,2,1]
    s=mats[:,0:2,0].norm()
    rot=torch.acos(mats[:,0,0]/s)
    #return torch.cat([x[:,None,...],y[:,None,...],rot[:,None,...],s[:,None,...]],dim=1)
    return torch.tensor([x,y,rot,s], requires_grad=True).cuda()

def convRelu(i, batchNormalization=False, leakyRelu=False, numChanIn=3, split=False):
    nc = numChanIn
    ks = [3, 3, 3, 3, 3, 3, 2]
    ps = [1, 1, 1, 1, 1, 1, 1]
    ss = [1, 1, 1, 1, 1, 1, 1]
    #nm = [64, 128, 256, 256, 512, 512, 512]
    nm = [64, 128, 256, 256, 512, 512, 512]

    cnn = nn.Sequential()

    nIn = nc if i == 0 else nm[i - 1]
    nOut = nm[i]
    if split:
        nOut=nOut//2
    conv = nn.Conv2d(nIn, nOut, ks[i], ss[i], ps[i])
    cnn.add_module('conv{0}'.format(i),conv)
    if batchNormalization:
        cnn.add_module('batchnorm{0}'.format(i), nn.InstanceNorm2d(nOut))
        # cnn.add_module('batchnorm{0}'.format(i), nn.BatchNorm2d(nOut))
    if leakyRelu:
        cnn.add_module('relu{0}'.format(i),
                       nn.LeakyReLU(0.1, inplace=True))
    else:
        cnn.add_module('relu{0}'.format(i), nn.ReLU(True))
    return cnn

def makeCnn(batchNorm,leakyReLU,numChanIn,split):

    cnn1 = nn.Sequential()
    cnn1.add_module('convRelu{0}'.format(0), convRelu(0, leakyRelu=leakyReLU, numChanIn=numChanIn))
    cnn1.add_module('pooling{0}'.format(0), nn.MaxPool2d(2, 2))
    cnn1.add_module('convRelu{0}'.format(1), convRelu(1))
    cnn1.add_module('pooling{0}'.format(1), nn.MaxPool2d(2, 2))
    cnn1.add_module('convRelu{0}'.format(2), convRelu(2, batchNorm, leakyReLU))
    cnn1.add_module('convRelu{0}'.format(3), convRelu(3))
    cnn1.add_module('pooling{0}'.format(2), nn.MaxPool2d(2, 2))
    if split:
        convReLU_shared = convRelu(4, batchNorm, leakyReLU, split=True)
        convReLU_forward = convRelu(4, batchNorm, leakyReLU, split=True)
        convReLU_back = convRelu(4, batchNorm, leakyReLU, split=True)
    else:
        convReLU_shared=None
        convReLU_forward=None
        convReLU_back=None
        cnn1.add_module('convRelu{0}'.format(4), convRelu(4, batchNorm, leakyReLU))
    cnn2 = nn.Sequential()
    cnn2.add_module('convRelu{0}'.format(5), convRelu(5))
    cnn2.add_module('pooling{0}'.format(3), nn.MaxPool2d(2, 2))
    cnn2.add_module('convRelu{0}'.format(6), convRelu(6, batchNorm, leakyReLU))
    cnn2.add_module('pooling{0}'.format(4), nn.MaxPool2d(2, 2))

    return cnn1, convReLU_shared, convReLU_forward, convReLU_back, cnn2

class LineFollower(BaseModel):
    def __init__(self, config, dtype=torch.cuda.FloatTensor):
        super(LineFollower, self).__init__(config)
        self.view_window_size = 32
        batchNorm = "batch_norm" in config and config['batch_norm']
        leakyReLU = "leaky_relu" in config and config['leaky_relu']
        if "angle_only" in config and config["angle_only"]:
            self.no_xy=True
            num_pos=1
        else:
            self.no_xy=False
            num_pos=3

        self.split_cnn_forback = "split_cnn_forback" in config and config['split_cnn_forback']
        self.pred_end = "pred_end" in config and config['pred_end']

        self.pred_scale = 'pred_scale' in config and config['pred_scale']
        if self.pred_scale:
            self.scale_index=num_pos
            num_pos+=1
        else:
            self.scale_index=None

        if "gray_scale" in config and config["gray_scale"]:
            colorCh=1
        else:
            colorCh=3
        pointCh=1 #confidence, (only, no rot, scale yet); we assume the point has been aligned according to its offset
        inputChannels = (colorCh+pointCh) if self.pred_end else colorCh
        
      
        position_linear = nn.Linear(512,num_pos)
        position_linear.weight.data.zero_()
        position_linear.bias.data[0:num_pos] = 0 #dont shift or rotate, no scale is zero as well
        if self.pred_scale:
            #self.scale_linear = nn.Linear(512,1)
            #self.scale_linear.weight.data.zero_()
            #self.scale_linear.bias.data[0] = 0 #scale is zero as well
            self.scale_root = 2 #nn.Parameter(torch.tensor(2,dtype=torch.float), requires_grad=True)
        if self.pred_end:
            self.end_linear = nn.Sequential(nn.Linear(512,1), nn.Sigmoid())

        if 'noise_scale' in config:
            self.noise_scale = config['noise_scale']
        else:
            self.noise_scale = 1
        
        if 'randomize_start' in config:
            self.randomizeStart = True
            self.mean_dist_x = config['randomize_start']['mean_dist_x']
            self.std_dist_x = config['randomize_start']['std_dist_x']
            self.mean_dist_y = config['randomize_start']['mean_dist_y']
            self.std_dist_y = config['randomize_start']['std_dist_y']
            self.mean_rot = config['randomize_start']['mean_rot']
            self.std_rot = config['randomize_start']['std_rot']
            self.mean_scale = config['randomize_start']['mean_rot']
            self.std_scale = config['randomize_start']['std_rot']

        
        if 'output_grid_size' in config:
            self.output_grid_size = output_grid_size['output_grid_size']
        else:
            self.output_grid_size=self.view_window_size

        self.dtype = dtype
        self.cnn1, self.shared_conv, self.forward_conv, self.backward_conv, self.cnn2 = makeCnn(batchNorm,leakyReLU, inputChannels, self.split_cnn_forback)
        self.position_linear = position_linear

        self.grid_gen = GridGen(self.view_window_size,self.view_window_size)

    def forward(self, image, start_position, forward, steps=None, all_positions=[], all_xy_positions=[], reset_interval=-1, randomize=False, negate_lw=False, skip_grid=False, allow_end_early=False, detected_end_points=None):

        #if reset_interval>0:
        #    reset_interval = random.randint(reset_interval-2,reset_interval+2)

        ##ttt=[]
        ##ttt2=[]

        batch_size = image.size(0)
        renorm_matrix = transformation_utils.compute_renorm_matrix(image)
        expanded_renorm_matrix = renorm_matrix.expand(batch_size,3,3)

        t = ((np.arange(self.output_grid_size) + 0.5) / float(self.output_grid_size))[:,None].astype(np.float32)
        t = np.repeat(t,axis=1, repeats=self.output_grid_size)
        t = torch.from_numpy(t).cuda()
        s = t.t()

        t = t[:,:,None]
        s = s[:,:,None]

        interpolations = torch.cat([
            (1-t)*s,
            (1-t)*(1-s),
            t*s,
            t*(1-s),
        ], dim=-1)

        view_window = torch.cuda.FloatTensor([
            [2,0,2],
            [0,2,0],
            [0,0,1]
        ]).expand(batch_size,3,3)

        step_bias = torch.cuda.FloatTensor([
            [1,0,-2],
            [0,1,0],
            [0,0,1]
        ]).expand(batch_size,3,3)

        invert = torch.cuda.FloatTensor([
            [-1,0,0],
            [0,-1,0],
            [0,0,1]
        ]).expand(batch_size,3,3)

        a_pt = torch.Tensor(
            [
                [0, 1,1],
                [0,-1,1]
            ]
        ).cuda()
        a_pt = a_pt.transpose(1,0)
        a_pt = a_pt.expand(batch_size, a_pt.size(0), a_pt.size(1))
        b_pt = torch.Tensor(
            [
                [-1,0,1],
                [ 1,0,1]
            ]
        ).cuda()
        b_pt = b_pt.transpose(1,0)
        b_pt = b_pt.expand(batch_size, b_pt.size(0), b_pt.size(1))

        if negate_lw:
            view_window = invert.bmm(view_window)


        view_window_imgs = []
        next_windows = []
        reset_windows = True
        end_preds = []
        for i in range(steps):

            if i%reset_interval != 0 or reset_interval==-1:
                p_0 = start_position#s[-1]

                if i == 0 and len(p_0.size()) == 3 and p_0.size()[1] == 3 and p_0.size()[2] == 3:
                    print('when does this hit?')
                    print(p_0)
                    current_window = p_0
                    reset_windows = False
                    next_windows.append(p_0)

            else:
                #p_0 = all_positions[i].type(self.dtype)
                if len(next_windows)>0:
                    w_0 = next_windows[-1]
                    cur_xy_pos = w_0.bmm(a_pt)
                    d_t, p_t, d_b, p_b = getMinimumDists(cur_xy_pos[0,:2,0],cur_xy_pos[0,:2,1],all_xy_positions, return_points=True) #all_positions[i].type(self.dtype)
                    d = p_t-p_b
                    scale = d.norm()/2
                    mx = (p_t[0]+p_b[0])/2.0
                    my = (p_t[1]+p_b[1])/2.0
                    theta = torch.atan2(d[0],d[1])
                    #theta = -torch.atan2(d[0],-d[1])
                    #print('d={}, scale={}, mx={}, my={}, theta={}'.format(d.size(),scale.size(),mx.size(),my.size(),theta.size()))
                    #print('w_0={}, cur_xy_pos={}, d={}, scale={}, mx={}, my={}, theta={}'.format(w_0.requires_grad,cur_xy_pos.requires_grad,d.requires_grad,scale.requires_grad,mx.requires_grad,my.requires_grad,theta.requires_grad))
                    #p_0 = torch.cat([mx,my,theta,scale,torch.ones_like(scale, requires_grad=True)])[None,...] #add batch dim
                    p_0 = torch.tensor([mx,my,theta,scale,1.0], requires_grad=True).cuda()[None,...] #add batch dim
                    #TODO may not requer grad
                else:
                    p_0 = all_positions[i].type(self.dtype) #this only occus an index 0 (?)
                reset_windows = True
                if randomize and (i!=0 or not self.randomizeStart):
                    add_noise = p_0.clone()
                    add_noise.data.zero_()
                    mul_moise = p_0.clone()
                    mul_moise.data.fill_(1.0)

                    add_noise[:,0].data.uniform_(-2*self.noise_scale, 2*self.noise_scale)
                    add_noise[:,1].data.uniform_(-2*self.noise_scale, 2*self.noise_scale)
                    add_noise[:,2].data.uniform_(-.1*self.noise_scale, .1*self.noise_scale)
                    if self.pred_scale:
                        mul_moise[:,3].data.uniform_(0.86*self.noise_scale, 1.15*self.noise_scale)

                    p_0 = p_0 * mul_moise + add_noise

            if i==0 and self.randomizeStart:
                add_noise = p_0.clone()
                add_noise.data.zero_()
                #mul_moise = p_0.clone()
                #mul_moise.data.fill_(1.0)

                add_noise[:,0].data.normal_(self.mean_dist_x, self.std_dist_x)
                add_noise[:,1].data.normal_(self.mean_dist_y, self.std_dist_y)
                add_noise[:,2].data.normal_(self.mean_rot, self.std_rot)
                #if self.pred_scale:
                add_noise[:,3].data.normal_(self.mean_scale, self.std_scale)

                p_0 = p_0 - add_noise #I calculated differences using targ-pred=dif, so we need to subtract (pred=targ-dif)

            if reset_windows:
                reset_windows = False

                current_window = transformation_utils.get_init_matrix(p_0)

                if len(next_windows) == 0:
                    next_windows.append(current_window)
            else:
                current_window = next_windows[-1].detach()


            crop_window = current_window.bmm(view_window)
            #I need the x,y cords from here

            resampled = get_patches(image, crop_window, self.grid_gen, allow_end_early)

            if resampled is None and i > 0:
                #get patches checks to see if stopping early is allowed
                break

            if resampled is None and i == 0:
                #Odd case where it start completely off of the edge
                #This happens rarely, but maybe should be more eligantly handled
                #in the future
                resampled = torch.zeros(crop_window.size(0), 3, self.view_window_size, self.view_window_size).type_as(image.data)

            if self.pred_end:
                #transform all detected end points to the resampled coordinates ([-1,1])
                #add any in the resampled image
                # shape of detected_end_points [instances, features] feautres:conf,x,y,rot,scale
                #detected_end_points_xy = torch.zeros(detected_end_points.size(0), 3)
                #detected_end_points_xy[:,0:2] = detected_end_points[:,1:3]
                detected_end_points_xy = detected_end_points[:,:,1:3]
                detected_end_points_xy = torch.cat([detected_end_points_xy,torch.ones(detected_end_points_xy.size(0),detected_end_points_xy.size(1),1).cuda()], dim=2)
                detected_end_points_xy = detected_end_points_xy.transpose(1,2) #so matrics mult is correct
                trans_detected_points = torch.bmm(b_inv(crop_window.data), detected_end_points_xy)
                valid_points =( (trans_detected_points[:,0,:]>=-1) & (trans_detected_points[:,0,:]<=1) &
                        (trans_detected_points[:,1,:]>=-1) & (trans_detected_points[:,1,:]<=1) )
                detected_img = torch.zeros(crop_window.size(0), 1, self.view_window_size,self.view_window_size).type_as(image.data) #1 channel for conf
                for b in range(crop_window.size(0)):
                    xys = (trans_detected_points[b,0:2,valid_points[b]]+1)/2 #get selected points on convert to range [0,1)
                    if xys.size(0)>0:
                        xys = ((self.view_window_size-1)*xys).round().type(torch.LongTensor) #convert to range [0,31]
                        confs = detected_end_points[0,valid_points[b],0]
                        detected_img[b,0,xys[1,:],xys[0,:]]=confs
                #TODO change rotation and scale according to the transformation and include them

                resampled = torch.cat([resampled,detected_img],dim=1)

                #meh, handle this in loss function
                ##is the correct end point in the window?
                #trans_end = torch.inverse(crop_window.data).bmm(end_point)
                #if trans_end[0]>=-1 and trans_end[0]<=1 and
                #    trans_end[1]>=-1 and trans_end[1]<=1:
                #    end_present.append(1)
                #else:
                #end_present.


            # Process Window CNN
            cnn_out = self.cnn1(resampled)
            if self.shared_conv is not None:
                shared_out = self.shared_conv(cnn_out)
                if forward:
                    part_out = self.forward_conv(cnn_out)
                else:
                    part_out = self.backward_conv(cnn_out)
                cnn_out = torch.cat([shared_out,part_out],dim=1)
            cnn_out = self.cnn2(cnn_out)
            #cnn_out = self.cnn(resampled)
            cnn_out = torch.squeeze(cnn_out, dim=2)
            cnn_out = torch.squeeze(cnn_out, dim=2)
            delta = self.position_linear(cnn_out)
            if self.pred_scale:
                #scale_out = self.scale_linear(cnn_out)
                #twos = 2*torch.ones_like(scale_out)
                twos = 2*torch.ones_like(delta[:,self.scale_index])
                #delta_scale = torch.pow(twos, scale_out)
                delta[:,self.scale_index] = torch.pow(twos, delta[:,self.scale_index].clone())
                #print('{}  {}'.format(len(next_windows),delta[0,self.scale_index]))
                #delta_scale = torch.pow(self.scale_root, scale_out)
            #else:
            #    delta_scale = None

            if self.pred_end:
                end_out = self.end_linear(cnn_out)
                end_preds.append(end_out)



            ##rint(delta)
            ##rint(delta_scale)
            next_window = transformation_utils.get_step_matrix(delta,self.no_xy,self.scale_index)
            ##rint('{} delta'.format(i))
            ##rint(next_window)
            next_window = next_window.bmm(step_bias)
            ##rint('{} delta step'.format(i))
            ##rint(next_window)
            if negate_lw:
                next_window = invert.bmm(next_window).bmm(invert)

            next_windows.append(current_window.bmm(next_window))
            ##rint('{} window'.format(i))
            ##rint(next_windows[-1])
            #if self.pred_end:



        grid_line = []
        mask_line = []
        line_done = []
        xy_positions = []
        xyrs_pos =[]


        for i in range(0, len(next_windows)-1):

            w_0 = next_windows[i]
            w_1 = next_windows[i+1]

            pts_0 = w_0.bmm(a_pt)
            pts_1 = w_1.bmm(a_pt)
            xy_positions.append(pts_0) #[[xU,xL],[yU,yL],[1,1]]
            xyrs_pos.append(get_xyrs(w_0))

            if skip_grid:
                continue

            pts = torch.cat([pts_0, pts_1], dim=2)

            grid_pts = expanded_renorm_matrix.bmm(pts)

            grid = interpolations[None,:,:,None,:] * grid_pts[:,None,None,:,:]
            grid = grid.sum(dim=-1)[...,:2]

            grid_line.append(grid)
        if len(next_windows)==1:
            w_0 = next_windows[0]
            pts_0 = w_0.bmm(a_pt)
            pts_1 = w_0.bmm(b_pt)
            xy_positions.append(pts_0)
            if not skip_grid:
                pts = torch.cat([pts_0, pts_1], dim=2)

                grid_pts = expanded_renorm_matrix.bmm(pts)

                grid = interpolations[None,:,:,None,:] * grid_pts[:,None,None,:,:]
                grid = grid.sum(dim=-1)[...,:2]

                grid_line.append(grid)
            

        xy_positions.append(pts_1)
        xyrs_pos.append(get_xyrs(next_windows[-1]))

        #print('pre-clamp {}, post-clamp {}'.format(['{:0.3f}'.format(v) for v in ttt],['{:0.3f}'.format(v) for v in ttt2]))

        if skip_grid:
            #grid_line = None
            return xy_positions, xyrs_pos, end_preds
        else:
            grid_line = torch.cat(grid_line, dim=1)

        return grid_line, view_window_imgs, next_windows, xy_positions, xyrs_pos, end_preds



def get_patches(image, crop_window, grid_gen, allow_end_early=False, end_points=None):


        pts = torch.FloatTensor([
            [-1.0, -1.0, 1.0, 1.0],
            [-1.0, 1.0, -1.0, 1.0],
            [ 1.0, 1.0,  1.0, 1.0]
        ]).type_as(image.data)[None,...]

        bounds = crop_window.matmul(pts)

        min_bounds, _ = bounds.min(dim=-1)
        max_bounds, _ = bounds.max(dim=-1)
        d_bounds = max_bounds - min_bounds
        floored_idx_offsets = torch.floor(min_bounds[:,:2].data).long()
        max_d_bounds = d_bounds.max(dim=0)[0].max(dim=0)[0]
        crop_size = torch.ceil(max_d_bounds).long()
        if image.is_cuda:
            crop_size = crop_size.cuda()
        w = crop_size.data.item() #[0]
        if w==0:
            w=1

        memory_space = torch.zeros(d_bounds.size(0), 3, w, w).type_as(image.data)
        translations = []
        N = transformation_utils.compute_renorm_matrix(memory_space)
        all_skipped = True

        for b_i in range(memory_space.size(0)):

            o = floored_idx_offsets[b_i]

            t = torch.cuda.FloatTensor([
                [1,0,-o[0]],
                [0,1,-o[1]],
                [0,0,    1]
            ]).expand(3,3)
            translations.append(N.mm(t)[None,...])

            skip_slice = False

            s_x = (o[0], o[0]+w)
            s_y = (o[1], o[1]+w)
            t_x = (0, w)
            t_y = (0, w)
            if o[0] < 0:
                s_x = (0, w+o[0])
                t_x = (-o[0], w)

            if o[1] < 0:
                s_y = (0, w+o[1])
                t_y = (-o[1], w)

            if o[0]+w >= image.size(2):
                s_x = (s_x[0], image.size(2))
                t_x = (t_x[0], t_x[0]+image.size(2) - s_x[0])

            if o[1]+w >= image.size(3):
                s_y = (s_y[1], image.size(3))
                t_y = (t_y[1], t_y[1]+image.size(3) - s_y[1])

            if s_x[0] >= s_x[1]:
                skip_slice = True

            if t_x[0] >= t_x[1]:
                skip_slice = True

            if s_y[0] >= s_y[1]:
                skip_slice = True

            if t_y[0] >= t_y[1]:
                skip_slice = True

            if not skip_slice:
                all_skipped = False
                i_s  = image[b_i:b_i+1, :, s_x[0]:s_x[1], s_y[0]:s_y[1]]  # I think this an optimization
                #print(i_s.size())
                #print(memory_space.size())
                memory_space[b_i:b_i+1, :, t_x[0]:t_x[1], t_y[0]:t_y[1]] = i_s

                #if end_points is not None:
                    #get all points in bounds
                    #transform points

        if all_skipped and allow_end_early:
            return None

        translations = torch.cat(translations, 0)
        grid = grid_gen(translations.bmm(crop_window))
        grid = grid[:,:,:,0:2] / grid[:,:,:,2:3]

        resampled = torch.nn.functional.grid_sample(memory_space.transpose(2,3), grid, mode='bilinear')

        #if end_points is None:
        return resampled
        #else:

